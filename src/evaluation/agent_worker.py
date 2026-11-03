"""Native runtime worker copied into a disposable, network-disabled container.

Only tool interfaces and task inputs cross into this process. Assertions, fault
schedules, provider keys and result logs remain with the host controller.
"""

import asyncio
import base64
import hashlib
import json
import os
import queue
import sys
import threading
import traceback
import uuid
from pathlib import Path

PREFIX = "@agent-channel@"
WORKSPACE = Path("/workspace")
AUDIT_ROOT = WORKSPACE
VISIBLE_WORKSPACE = "/workspace"


class Channel:
    def __init__(self):
        self.commands = queue.Queue()
        self.pending = {}
        self.lock = threading.RLock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in sys.stdin:
                value = json.loads(line)
                if "reply_to" in value:
                    with self.lock:
                        target = self.pending.get(value["reply_to"])
                    if target is None:
                        raise RuntimeError("Unmatched controller reply")
                    target.put(value)
                else:
                    self.commands.put(value)
        except Exception as exc:
            self.commands.put({"op": "protocol_error", "error": str(exc)})
        finally:
            self.commands.put({"op": "close"})
            with self.lock:
                for target in self.pending.values():
                    target.put({"error": "Controller disconnected"})

    def emit(self, value):
        with self.lock:
            print(PREFIX + json.dumps(value, ensure_ascii=False, default=str), flush=True)

    def rpc(self, kind, payload):
        ident = uuid.uuid4().hex
        reply = queue.Queue()
        with self.lock:
            self.pending[ident] = reply
        try:
            self.emit({"event": "rpc", "id": ident, "kind": kind, "payload": payload})
            result = reply.get()
            if "error" in result:
                raise RuntimeError(result["error"])
            return result["result"]
        finally:
            with self.lock:
                self.pending.pop(ident, None)


def snapshot(root=None):
    root = WORKSPACE if root is None else root
    result = {".": {"kind": "directory"}}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = {"kind": "symlink", "target": os.readlink(path)}
        elif path.is_dir():
            result[name] = {"kind": "directory"}
        elif path.is_file():
            raw = path.read_bytes()
            item = {"kind": "file", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            try:
                item["text"] = raw.decode("utf-8")
            except UnicodeDecodeError:
                item["data_base64"] = base64.b64encode(raw).decode("ascii")
            result[name] = item
    return result


def auxiliary_snapshot():
    """Observe other native task directories without duplicating the task root."""
    if AUDIT_ROOT == WORKSPACE:
        return {}
    relative = WORKSPACE.relative_to(AUDIT_ROOT).as_posix()
    return {
        name: value
        for name, value in snapshot(AUDIT_ROOT).items()
        if name not in {".", relative} and not name.startswith(relative + "/")
    }


def workspace_path(name):
    # Fixture paths use the same benchmark placeholder contract as host assertions.
    # Map to the real native workspace, not the model-visible virtual path.
    name = name.replace("{WORKSPACE}", str(WORKSPACE)).replace("\\", "/")
    path = (WORKSPACE / name).resolve()
    if not path.is_relative_to(WORKSPACE) or path == WORKSPACE:
        raise ValueError("Fixture path escapes task workspace")
    return path


def mutate_files(operations):
    """Controller-owned fixture changes, separate from native agent tool calls."""
    for op in operations:
        path = workspace_path(op["path"])
        kind = op["type"]
        if kind in {"write_file", "append_file"}:
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = (
                base64.b64decode(op["data_base64"], validate=True)
                if "data_base64" in op
                else op.get("content", "").replace("{WORKSPACE}", VISIBLE_WORKSPACE).encode("utf-8")
            )
            with path.open("ab" if kind == "append_file" else "wb") as stream:
                stream.write(raw)
        elif kind == "mkdir":
            path.mkdir(parents=True, exist_ok=True)
        elif kind == "delete_file":
            path.unlink(missing_ok=True)
        elif kind == "chmod":
            mode = op["mode"]
            path.chmod(int(mode, 8) if isinstance(mode, str) else mode)
        else:
            raise ValueError(f"Unsupported fixture operation {kind}")


def install_model_relay(channel):
    """Keep wire payloads intact; no provider secret or network in the container.

    Streaming bodies are relayed as complete SSE byte sequences. Host capture
    retains arrival timestamps; delivery timing to the native client is buffered.
    """
    import importlib

    for name in ("httpx", "httpx2"):
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
            continue
        _install_http_relay(module, channel)


def _install_http_relay(httpx, channel):
    """Support each native SDK's installed HTTPX generation without substituting it."""

    original_sync = httpx.Client._send_single_request
    original_async = httpx.AsyncClient._send_single_request

    def forward(request):
        if request.url.host != "model.local" or request.url.path != "/v1/chat/completions":
            raise ValueError("Relay accepts only configured model chat completions")
        result = channel.rpc("inference", {"body": json.loads(request.content)})
        headers = {
            k: v
            for k, v in result["headers"].items()
            if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        }
        return httpx.Response(
            result["status"],
            headers=headers,
            content=base64.b64decode(result["body_base64"]),
            request=request,
        )

    def sync(client, request):
        if request.url.host == "model.local":
            request.read()
            return forward(request)
        return original_sync(client, request)

    async def asynchronous(client, request):
        if request.url.host == "model.local":
            await request.aread()
            return await asyncio.to_thread(forward, request)
        return await original_async(client, request)

    httpx.Client._send_single_request = sync
    httpx.AsyncClient._send_single_request = asynchronous


def main():
    global WORKSPACE, VISIBLE_WORKSPACE, AUDIT_ROOT
    # The bridge and the command loop must share the same workspace state.
    sys.modules["agent_worker"] = sys.modules[__name__]
    channel = Channel()
    install_model_relay(channel)
    runtime = None
    channel.emit({"event": "ready", "workspace": str(WORKSPACE)})
    while True:
        command = channel.commands.get()
        ident = command.get("id")
        try:
            op = command["op"]
            if op == "close":
                if runtime:
                    runtime.close()
                if ident:
                    channel.emit({"event": "result", "id": ident, "result": {"closed": True}})
                return
            if op == "init":
                from native_agent_bridges import create_runtime

                runtime = create_runtime(command["config"], channel)
                WORKSPACE = getattr(runtime, "workspace_root", WORKSPACE)
                AUDIT_ROOT = getattr(runtime, "audit_root", WORKSPACE)
                VISIBLE_WORKSPACE = getattr(runtime, "visible_workspace", str(WORKSPACE))
                result = {
                    "tools": runtime.catalog(),
                    "workspace": VISIBLE_WORKSPACE,
                    "physical_workspace": str(WORKSPACE),
                    "runtime_profile": runtime.profile(),
                }
            elif op == "files":
                mutate_files(command["operations"])
                result = snapshot()
            elif op == "snapshot":
                result = snapshot()
            elif op == "auxiliary_snapshot":
                result = auxiliary_snapshot()
            elif op == "run":
                result = runtime.run(command["prompt"])
            elif op == "reference":
                result = runtime.reference(
                    command["prompt"], command["actions"], command["final_response"]
                )
            elif op == "export":
                result = runtime.export()
            elif op == "history":
                result = runtime.history()
            elif op == "import":
                result = runtime.restore(command["state"])
            elif op == "truncate_history":
                result = runtime.truncate(command["keep_last_n_messages"])
            else:
                raise ValueError(f"Unsupported worker command {op}")
            channel.emit({"event": "result", "id": ident, "result": result})
        except Exception as exc:
            channel.emit(
                {
                    "event": "result",
                    "id": ident,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            )


if __name__ == "__main__":
    main()
