"""Host controller for network-disabled native-agent containers.

No host directories or Docker socket are mounted. Docker copies only the bridge
code in; protocol messages carry fixtures, tool interfaces and model responses.
"""

import asyncio
import base64
import json
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

PREFIX = "@agent-channel@"
ROOT = Path(__file__).resolve().parent


class NativeExecutionError(RuntimeError):
    """The native process reported an exception; this is not a verifier verdict."""

    def __init__(self, operation, message, traceback):
        self.operation, self.native_message, self.native_traceback = operation, message, traceback
        super().__init__(message + "\n" + traceback)


def docker_command(image, name, *, command=None):
    if not image or image.startswith("-"):
        raise ValueError("Invalid runtime image")
    return [
        "docker",
        "create",
        "--name",
        name,
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--memory",
        "4g",
        "--cpus",
        "2",
        "--init",
        "-i",
        "--user",
        "10001:10001",
        "--workdir",
        "/workspace",
        image,
        *(command if command is not None else ["python", "-u", "/opt/agent_worker.py"]),
    ]


class AgentContainer:
    def __init__(self, image, directory, inference, mock, observe):
        self.name = "agent-work-" + uuid.uuid4().hex[:12]
        self.directory = Path(directory)
        self.inference, self.mock, self.observe = inference, mock, observe
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=16)
        self.process = None
        self.stderr = None
        self.futures = []
        self.created = False
        self.closed = False
        self.cancel_inference = None
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self._docker(*docker_command(image, self.name)[1:])
            self.created = True
            self._docker("cp", str(ROOT / "agent_worker.py"), self.name + ":/opt/agent_worker.py")
            self._docker(
                "cp",
                str(ROOT / "native_agent_bridges.py"),
                self.name + ":/opt/native_agent_bridges.py",
            )
        except Exception:
            self.close()
            raise

    @staticmethod
    def _docker(*args):
        return subprocess.run(
            ["docker", *args],
            check=True,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout

    def start(self):
        """Attach after the bridge has been copied into a stopped container."""
        self.stderr = (self.directory / "native_stderr.log").open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            ["docker", "start", "-a", "-i", self.name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        value = self._next()
        if value.get("event") != "ready":
            raise RuntimeError(f"Worker did not become ready: {value}")

    def _send(self, value):
        with self.lock:
            self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def _next(self):
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError(
                    f"Agent container stopped (exit={self.process.poll()}); see native_stderr.log"
                )
            if line.startswith(PREFIX):
                return json.loads(line[len(PREFIX) :])
            with (self.directory / "native_stdout.log").open("a", encoding="utf-8") as log:
                log.write(line)

    def _rpc(self, value):
        try:
            handler = (
                self.inference
                if value["kind"] == "inference"
                else self.mock
                if value["kind"] == "mock"
                else None
            )
            if handler is None:
                raise ValueError("Unsupported RPC kind")
            result = handler(value["payload"])
            self._send({"reply_to": value["id"], "result": result})
        except Exception as exc:
            self._send({"reply_to": value["id"], "error": f"{type(exc).__name__}: {exc}"})
            raise

    def call(self, op, **kwargs):
        ident = uuid.uuid4().hex
        self._send({"op": op, "id": ident, **kwargs})
        while True:
            event = self._next()
            if event.get("event") == "rpc":
                self.futures.append(self.pool.submit(self._rpc, event))
            elif event.get("event") == "result":
                if event["id"] != ident:
                    raise RuntimeError("Mismatched command result")
                if event.get("error") and self.cancel_inference:
                    self.cancel_inference()
                failures = []
                for future in self.futures:
                    try:
                        future.result()
                    except Exception as exc:
                        failures.append(exc)
                self.futures.clear()
                if event.get("error"):
                    error = NativeExecutionError(op, event["error"], event.get("traceback", ""))
                    if failures:
                        raise error from ExceptionGroup("Outstanding native RPC failures", failures)
                    raise error
                if failures:
                    raise ExceptionGroup("Native RPC failures", failures)
                return event["result"]
            else:
                self.observe(event)

    def close(self):
        if self.closed:
            return
        self.closed = True
        errors = []
        try:
            if self.process and self.process.poll() is None:
                # Shutdown can flush native memory through the model. Keep
                # serving and recording RPCs until the runtime acknowledges it.
                self.call("close")
        except Exception as exc:
            errors.append(exc)
        finally:
            if self.cancel_inference:
                self.cancel_inference()
            if self.created:
                try:
                    self._docker("rm", "-f", self.name)
                except Exception as exc:
                    errors.append(exc)
            if self.process:
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    # Stop only our Docker attach client after container removal.
                    self.process.kill()
                    self.process.wait(timeout=10)
                for stream in (self.process.stdin, self.process.stdout):
                    stream.close()
            if self.stderr:
                self.stderr.close()
            self.pool.shutdown(wait=True)
        if errors:
            raise ExceptionGroup("Native container cleanup failed", errors)


def inference_relay(target, key, payload, *, cancel_event=None):
    body = payload["body"]
    if body.get("model") != target["model"]:
        raise ValueError("Native runtime requested a different model")
    if cancel_event is not None:
        return asyncio.run(_cancellable_inference(target, key, body, cancel_event))
    with httpx.Client(timeout=httpx.Timeout(180, read=None)) as client:
        response = client.post(
            target["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + key},
            json=body,
        )
        return {
            "status": response.status_code,
            "headers": dict(response.headers),
            "body_base64": base64.b64encode(response.content).decode("ascii"),
        }


async def _cancellable_inference(target, key, body, cancel_event):
    """Cancel I/O only when the native runtime has failed or is being closed.

    Successful inference keeps its existing unlimited read duration and payload.
    Async cancellation closes the transport and remains visible to API capture.
    """
    if cancel_event.is_set():
        raise RuntimeError("Inference relay cancelled after native runtime termination")

    async def cancelled():
        while not cancel_event.is_set():
            await asyncio.sleep(0.05)

    async with httpx.AsyncClient(timeout=httpx.Timeout(180, read=None)) as client:
        request = asyncio.create_task(
            client.post(
                target["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json=body,
            )
        )
        cancellation = asyncio.create_task(cancelled())
        try:
            done, _ = await asyncio.wait(
                (request, cancellation), return_when=asyncio.FIRST_COMPLETED
            )
            if request not in done:
                raise RuntimeError("Inference relay cancelled after native runtime termination")
            response = await request
            return {
                "status": response.status_code,
                "headers": dict(response.headers),
                "body_base64": base64.b64encode(response.content).decode("ascii"),
            }
        finally:
            request.cancel()
            cancellation.cancel()
            await asyncio.gather(request, cancellation, return_exceptions=True)
