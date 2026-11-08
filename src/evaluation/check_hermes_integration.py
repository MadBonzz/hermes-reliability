"""Offline check against the installed Hermes registry; does not invoke a model."""

import json
import logging
import os
import shlex
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

if __package__ in (None, ""):
    __package__ = "src.evaluation"
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from . import utils
from .api_trace import APITrace
from .control_observer import NativeControlObserver
from .execution_isolation import file_tool_guard, isolated_python
from .mock_registry import DynamicMockToolManager


def main():
    with tempfile.TemporaryDirectory(prefix="hermes_integration_") as temporary:
        with utils.hermes_run_environment(Path(temporary) / "home"):
            try:
                return run_checks(Path(temporary))
            finally:
                # Hermes's process-wide rotating handlers otherwise hold Windows locks
                # until interpreter exit, after TemporaryDirectory tries to remove home.
                logging.shutdown()


def check_local_transport(temporary_root):
    """Real Hermes + local HTTP stub: retry, streamed tool call, result and final answer.

    No model server is involved. Keep a marker in the ancestor directory to verify
    the isolated profile suppresses coding workspace context even inside a project.
    """
    (temporary_root / "pyproject.toml").write_text('[project]\nname="unrelated-parent"\n')
    workspace = temporary_root / "transport_task"
    workspace.mkdir()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_body(self, status, content_type, body):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if status == 503:
                self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            body = {
                "data": [{"id": "eval-offline-probe", "object": "model", "max_model_len": 131072}]
            }
            self.send_body(200, "application/json", json.dumps(body).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if len(requests) == 1:
                self.send_body(
                    503, "application/json", b'{"error":{"message":"temporary probe failure"}}'
                )
                return
            message = {"role": "assistant", "content": "done"}
            finish = "stop"
            if len(requests) == 2:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "probe-write",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "probe.txt", "content": "ok"}),
                            },
                        }
                    ],
                }
                finish = "tool_calls"
            base = {"id": "probe-completion", "created": 1, "model": "eval-offline-probe"}
            if body.get("stream"):
                delta = {k: v for k, v in message.items() if v is not None}
                if "tool_calls" in delta:
                    delta["tool_calls"][0]["index"] = 0
                chunks = [
                    {
                        **base,
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    },
                    {
                        **base,
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    },
                ]
                payload = (
                    "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
                )
                self.send_body(200, "text/event-stream", payload.encode())
            else:
                result = {
                    **base,
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                }
                self.send_body(200, "application/json", json.dumps(result).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    agent = None
    trace = temporary_root / "transport_api.jsonl"
    try:
        with (
            utils.hermes_workspace(workspace, "offline_transport_probe"),
            APITrace(trace, run_id="offline", scenario_id="transport"),
        ):
            agent = utils.instantiate_hermes_agent(
                f"http://127.0.0.1:{server.server_port}/v1",
                "eval-offline-probe",
                "local-probe",
                4,
                workspace,
            )
            result = agent.run_conversation(
                user_message="Create probe.txt containing ok in the current working directory.",
                conversation_history=[],
                task_id="offline_transport_probe",
            )
            assert not result.get("error"), result
            assert (workspace / "probe.txt").read_text() == "ok"
            assert len(requests) >= 3, requests
            assert any(m.get("role") == "tool" for m in requests[-1]["messages"])
            system = "\n".join(
                str(m.get("content", ""))
                for m in requests[0]["messages"]
                if m.get("role") == "system"
            )
            assert "Workspace (snapshot at session start" not in system, system
            assert "Operator instructions (from config)" not in system, system
            assert "Current working directory: " + str(workspace) in system, system
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        assert any(e.get("status_code") == 503 for e in events), "Retry was not captured"
        assert any(e["event"] == "api_stream_event" for e in events), (
            "No streamed response captured"
        )
        assert events[-1]["event"] == "api_capture_end"
        print("PASS: isolated prompt, HTTP retry, streamed tool execution and API evidence")
    finally:
        if agent is not None and callable(getattr(agent, "close", None)):
            agent.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def check_python_environments(temporary_root):
    """Real terminal aliases install into one phase and do not populate the next."""
    wheel = temporary_root / "isolated_probe-0.0.1-py3-none-any.whl"
    files = {
        "isolated_probe/__init__.py": "VALUE = 1\n",
        "isolated_probe-0.0.1.dist-info/METADATA": "Metadata-Version: 2.1\nName: isolated-probe\nVersion: 0.0.1\n",
        "isolated_probe-0.0.1.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: integration-check\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    files["isolated_probe-0.0.1.dist-info/RECORD"] = "".join(
        f"{name},,\n" for name in [*files, "isolated_probe-0.0.1.dist-info/RECORD"]
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    original = dict(os.environ)
    for number in (1, 2):
        root = temporary_root / f"python_phase_{number}"
        workspace = root / "workspace"
        workspace.mkdir(parents=True)
        task_id = f"python_environment_{number}"
        with isolated_python(root), utils.hermes_workspace(workspace, task_id):

            def terminal(command):
                result = json.loads(
                    utils.registry.dispatch(
                        "terminal", {"command": command, "timeout": 60}, task_id=task_id
                    )
                )
                assert result.get("exit_code") == 0, result
                return result["output"]

            inspect = (
                "import sys,json,importlib.util; "
                "print(json.dumps({'prefix':sys.prefix,'installed':"
                "importlib.util.find_spec('isolated_probe') is not None}))"
            )
            for alias in ("python", "python3"):
                observed = json.loads(terminal(f"{alias} -c {shlex.quote(inspect)}"))
                assert Path(observed["prefix"]).resolve() == (root / "python_env").resolve()
                assert not observed["installed"], observed
            if number == 1:
                terminal(f"pip3 install --no-index {shlex.quote(wheel.as_posix())}")
                assert json.loads(terminal(f"python -c {shlex.quote(inspect)}"))["installed"]
                terminal("uv pip show isolated-probe")
        assert dict(os.environ) == original
    print("PASS: native terminal Python/pip/uv use independent per-phase environments")


def run_checks(temporary_root):
    config = utils.load_eval_config(utils.PROJECT_ROOT / ".env")
    utils.load_hermes(config.get("HERMES_AGENT_ROOT"))
    check_python_environments(temporary_root)
    original = dict(utils.registry._tools)
    guarded = temporary_root / "guarded"
    guarded.mkdir()
    outside = temporary_root / "private-log.txt"
    outside.write_text("private-log-canary")
    with (
        utils.hermes_workspace(guarded, "guard_probe"),
        file_tool_guard(utils.registry, guarded, utils.tool_working_directory),
    ):
        for name, arguments in (
            ("read_file", {"path": str(outside)}),
            ("search_files", {"path": str(temporary_root), "pattern": "canary"}),
            ("write_file", {"path": str(outside), "content": "changed"}),
        ):
            result = utils.registry.dispatch(name, arguments, task_id="guard_probe")
            assert json.loads(result)["code"] == "EVAL_PATH_DENIED", result
        result = utils.registry.dispatch(
            "write_file", {"path": "normal.txt", "content": "task data"}, task_id="guard_probe"
        )
        assert (guarded / "normal.txt").read_text() == "task data", result
        assert "task data" in utils.registry.dispatch(
            "read_file", {"path": "normal.txt"}, task_id="guard_probe"
        )
        from tools.terminal_tool import record_session_cwd

        nested = guarded / "nested"
        nested.mkdir()
        record_session_cwd("guard_probe", str(nested))
        utils.registry.dispatch(
            "write_file", {"path": "nested.txt", "content": "nested data"}, task_id="guard_probe"
        )
        assert (nested / "nested.txt").read_text() == "nested data"
    assert outside.read_text() == "private-log-canary"
    assert utils.registry._tools == original
    print("PASS: file path guard blocks evaluator evidence and permits task artifacts")
    observer_workspace = temporary_root / "native_observer"
    observer_workspace.mkdir()
    (observer_workspace / "unread.txt").write_text("original")
    observed = []

    def record_control(event, **fields):
        observed.append({"event": event, **fields})

    from tools import file_tools

    original_blocker = file_tools._stale_overwrite_blocker
    with (
        utils.hermes_workspace(observer_workspace, "observer_probe"),
        NativeControlObserver() as observer,
    ):
        for call_id, path in (("allowed", "new.txt"), ("blocked", "unread.txt")):
            observer.start_call(record_control, call_id, "write_file")
            try:
                result = json.loads(
                    utils.registry.dispatch(
                        "write_file",
                        {"path": path, "content": "new data"},
                        task_id="observer_probe",
                    )
                )
            finally:
                observer.end_call(call_id)
            if call_id == "blocked":
                assert result.get("stale_write_blocked"), result
            else:
                assert not result.get("error"), result
        assert not observer.errors, observer.errors
        assert observer.manifest["source_sha256"]
    assert file_tools._stale_overwrite_blocker is original_blocker
    decisions = {
        e["call_id"]: e["disposition"]
        for e in observed
        if e["event"] == "native_control_result" and e["control"] == "whole_file_overwrite"
    }
    assert decisions == {"allowed": "no_refusal_returned", "blocked": "refusal_returned"}, decisions
    assert (observer_workspace / "unread.txt").read_text() == "original"
    print(
        "PASS: native control observer records allowed and refused checks without changing outcomes"
    )
    specs = [
        {
            "name": "eval_probe",
            "description": "Offline integration probe",
            "parameters": {"type": "object", "properties": {}},
            "behavior": {"type": "static_return"},
        }
    ]
    service_probe = {
        "name": "eval_service_probe",
        "description": "Offline stateful-service integration probe",
        "parameters": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}, "amount": {"type": "integer"}},
            "required": ["request_id", "amount"],
            "additionalProperties": False,
        },
        "behavior": {
            "type": "stateful_service",
            "service": "probe",
            "operation": "idempotent_append",
            "initial_state": {"records": []},
            "idempotency_field": "request_id",
            "error_when_flag_set": "lost_ack",
            "fail_before_write": False,
            "clear_flag_after_first_error": True,
            "return_payload": {"status": "ok", "receipt_id": "probe-receipt"},
        },
    }
    print("Registered tools:", len(original))
    for mocks in (specs + [service_probe], [], specs):
        manager = DynamicMockToolManager(
            Path.cwd(),
            {},
            registry=utils.registry,
            toolsets=utils.toolsets,
            model_tools=utils.model_tools,
        )
        try:
            manager.snapshot_registry()
            manager.register_scenario_mocks(mocks)
            definitions = utils.model_tools.get_tool_definitions(
                enabled_toolsets=["terminal", "file"], quiet_mode=True
            )
            # The runtime may defer custom tools; the runner explicitly attaches mocks.
            agent = SimpleNamespace(
                tools=definitions, valid_tool_names={t["function"]["name"] for t in definitions}
            )
            manager.attach_to_agent_instance(agent)
            names = {t["function"]["name"] for t in agent.tools}
            assert {"terminal", "read_file", "write_file"} <= names, names
            assert ("eval_probe" in names) == bool(mocks), names
            if mocks:
                entry = utils.registry._tools["eval_probe"]
                assert entry.handler({}) == '{"status": "ok"}'
            if service_probe in mocks:
                assert "eval_service_probe" in names
                manager.fault_flags["lost_ack"] = True
                handler = utils.registry._tools["eval_service_probe"].handler
                args = {"request_id": "probe-operation", "amount": 25}
                assert json.loads(handler(args))["status"] == "error"
                replay = json.loads(handler(args))
                assert replay["receipt_id"] == "probe-receipt" and replay["replayed"]
                evidence = manager.services.snapshot()
                assert evidence["services"]["probe"]["records"] == [args]
                assert len(evidence["effects"]) == 1
                assert (
                    json.loads(handler({"request_id": "bad", "amount": True}))["status"] == "error"
                )
        finally:
            manager.cleanup()
        assert utils.registry._tools == original
        restored = {
            t["function"]["name"]
            for t in utils.model_tools.get_tool_definitions(
                enabled_toolsets=["terminal", "file"], quiet_mode=True
            )
        }
        assert "eval_probe" not in restored
        assert "eval_service_probe" not in restored
        assert {"terminal", "read_file", "write_file"} <= restored
        print("PASS: mock count", len(mocks), "| restored tools", len(original))
    from agent.runtime_cwd import resolve_agent_cwd
    from tools.file_tools import read_file_tool, write_file_tool
    from tools.terminal_tool import _resolve_container_task_id, get_session_cwd, terminal_tool

    original_cwd = resolve_agent_cwd()
    # Relative paths must follow each scenario, even after another scenario has
    # warmed the actual Hermes file/terminal caches. Exercise exception cleanup too.
    with tempfile.TemporaryDirectory(prefix="cwd_probe_", dir=temporary_root) as temporary:
        for number in range(3):
            workspace = Path(temporary).resolve() / str(number)
            workspace.mkdir()
            task = "eval_workspace_probe_" + workspace.parent.name + "_" + str(number)
            try:
                with utils.hermes_workspace(workspace, task):
                    assert resolve_agent_cwd() == workspace
                    assert _resolve_container_task_id(task) == task
                    value = "scenario-" + str(number)
                    written = json.loads(write_file_tool("relative.txt", value, task_id=task))
                    assert (workspace / "relative.txt").read_text() == value, written
                    assert value in read_file_tool("relative.txt", task_id=task)
                    result = json.loads(terminal_tool("pwd", task_id=task, timeout=15))
                    expected = workspace.as_posix()
                    if os.name == "nt":
                        expected = "/" + expected[0].lower() + expected[2:]
                    assert (
                        result["exit_code"] == 0
                        and result["output"].strip().lower() == expected.lower()
                    ), result
                    if number == 1:
                        raise RuntimeError("intentional cleanup probe")
            except RuntimeError as exc:
                if str(exc) != "intentional cleanup probe":
                    raise
            assert get_session_cwd(task) is None
            assert resolve_agent_cwd() == original_cwd
            print("PASS: isolated relative file/terminal paths and cleanup", number)
    check_local_transport(temporary_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
