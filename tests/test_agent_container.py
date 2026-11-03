"""Host isolation and protocol checks; native integration runs require Docker."""

import asyncio
import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from src.evaluation import agent_worker
from src.evaluation.agent_container import (
    AgentContainer,
    NativeExecutionError,
    docker_command,
    inference_relay,
)
from src.evaluation.artifact_checks import evaluate_artifact_check
from src.evaluation.audit import audit_campaign
from src.evaluation.eval_container_worker import execute, materialize
from src.evaluation.evaluation_metrics import efficiency
from src.evaluation.runtime_support import read_jsonl
from src.orchestrator.container_worker import main as native_validation_main
from src.orchestrator.container_worker import reference_diagnostics
from src.orchestrator.transcripts import render_phase
from src.orchestrator.workspace import Workspace


class ContainerProtocolTests(unittest.TestCase):
    def test_cleanup_failure_keeps_nested_cause_and_prior_gate_outcome(self):
        class CleanupFailure:
            def __init__(self, *args):
                pass

            def start(self):
                pass

            def call(self, op, **kwargs):
                if op == "init":
                    return {"workspace": "/workspace", "runtime_profile": {}, "tools": []}
                if op in {"snapshot", "auxiliary_snapshot"}:
                    return {}
                if op == "history":
                    return []
                if op == "run":
                    return {
                        "completed": True,
                        "native_status": "finished",
                        "final_response": "Done",
                    }
                raise AssertionError(op)

            def close(self):
                raise ExceptionGroup(
                    "Native container cleanup failed",
                    [
                        NativeExecutionError(
                            "close", "memory flush did not complete", "native stack"
                        )
                    ],
                )

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("src.evaluation.eval_container_worker.AgentContainer", CleanupFailure),
        ):
            root = Path(temporary)
            report = execute(
                {
                    "scenario": {
                        "id": "cleanup_case",
                        "name": "Cleanup case",
                        "property_tested": "Evidence",
                        "turns": [{"turn_number": 1, "prompt": "Confirm receipt."}],
                    },
                    "output": str(root / "evidence"),
                    "runtime_root": str(root / "runtime"),
                    "runtime_image": "stub",
                    "agent_kind": "deerflow",
                    "run_id": "test",
                    "model": "stub",
                    "base_url": "https://model.invalid/v1",
                }
            )
            self.assertTrue(report["turns"][0]["gate_passed"])
            self.assertEqual(report["status_before_cleanup"], "passed")
            self.assertEqual(report["status"], "error")
            self.assertIn("memory flush did not complete", report["cleanup_error"]["traceback"])
            feedback = reference_diagnostics(report)
            self.assertEqual(feedback["cleanup_error"], report["cleanup_error"])
            self.assertEqual(feedback["failed_reference_turns"], [])
            rendered = render_phase(root / "evidence/report.json")
            self.assertIn("Phase-level errors", rendered)
            self.assertIn("memory flush did not complete", rendered)

    def test_auxiliary_snapshot_preserves_sibling_outputs_without_duplicate_task_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "workspace"
            task.mkdir()
            (task / "source.txt").write_text("input", encoding="utf-8")
            (root / "outputs").mkdir()
            (root / "outputs/report.txt").write_text("result", encoding="utf-8")
            with (
                patch.object(agent_worker, "WORKSPACE", task),
                patch.object(agent_worker, "AUDIT_ROOT", root),
            ):
                self.assertEqual(agent_worker.snapshot()["source.txt"]["text"], "input")
                auxiliary = agent_worker.auxiliary_snapshot()
                self.assertEqual(auxiliary["outputs/report.txt"]["text"], "result")
                self.assertFalse(any(name.startswith("workspace") for name in auxiliary))

    def test_native_failure_retains_attempts_and_state_evidence(self):
        class FailingRuntime:
            def __init__(self, image, directory, inference, mock, observe):
                self.observe = observe

            def start(self):
                pass

            def close(self):
                pass

            def call(self, op, **kwargs):
                if op == "init":
                    return {
                        "workspace": "/workspace",
                        "runtime_profile": {"framework": "openhands"},
                        "tools": [],
                    }
                if op == "history":
                    return []
                if op == "snapshot":
                    return {".": {"kind": "directory"}}
                if op == "auxiliary_snapshot":
                    return {}
                if op == "run":
                    self.observe(
                        {
                            "event": "tool_start",
                            "id": "attempt-1",
                            "name": "terminal",
                            "arguments": {"command": "false"},
                            "workspace": {},
                        }
                    )
                    self.observe(
                        {
                            "event": "tool_complete",
                            "id": "attempt-1",
                            "name": "terminal",
                            "result": "exit 1",
                            "native_result": {
                                "observation": {"is_error": False, "exit_code": 1, "timeout": False}
                            },
                            "workspace": {},
                        }
                    )
                    raise NativeExecutionError("run", "native runtime stopped", "native traceback")
                raise AssertionError(op)

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("src.evaluation.eval_container_worker.AgentContainer", FailingRuntime),
        ):
            root = Path(temporary)
            report = execute(
                {
                    "scenario": {
                        "id": "case",
                        "name": "Case",
                        "property_tested": "Recovery",
                        "turns": [{"turn_number": 1, "prompt": "Update the report."}],
                    },
                    "output": str(root / "evidence"),
                    "runtime_root": str(root / "runtime"),
                    "runtime_image": "stub",
                    "agent_kind": "openhands",
                    "run_id": "test",
                    "model": "stub",
                    "base_url": "https://model.invalid/v1",
                }
            )
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["harness_errors"], [])
            self.assertEqual(report["runtime_errors"][0]["operation"], "run")
            turn = report["turns"][0]
            self.assertEqual(turn["tool_calls"][0]["id"], "attempt-1")
            self.assertTrue(turn["execution_events"][0]["completed"])
            self.assertEqual(
                turn["execution_events"][0]["native_observation_status"],
                {"is_error": False, "exit_code": 1, "timeout": False},
            )
            self.assertEqual(report["efficiency"]["failed_tool_results"], 1)
            events = read_jsonl(turn["trace_path"])
            self.assertEqual(
                [e["event"] for e in events if e["event"].startswith("tool_state_")],
                ["tool_state_before", "tool_state_after"],
            )
            self.assertEqual(
                next(e for e in events if e["event"] == "turn_start")["input_history"], []
            )
            self.assertIn("workspace_after_agent", turn)

    def test_assertion_mirror_rejects_unsafe_artifacts_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "keep.txt").write_text("unchanged", encoding="utf-8")
            for name, kind in (("../escape", "file"), ("D:/escape", "file"), ("link", "symlink")):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    materialize(root, {name: {"kind": kind, "text": "bad"}})
                self.assertEqual((root / "keep.txt").read_text(encoding="utf-8"), "unchanged")

    def test_container_has_no_mount_network_or_provider_secret(self):
        command = docker_command("agent-eval-openhands:pinned", "agent-work-test")
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertEqual(command[command.index("--user") + 1], "10001:10001")
        self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
        for forbidden in ("--volume", "-v", "--mount", "--env", "-e", "--privileged"):
            self.assertNotIn(forbidden, command)
        self.assertIn("no-new-privileges", command)
        with self.assertRaises(ValueError):
            docker_command("--privileged", "agent-work-test")

    def test_relay_preserves_payload_and_does_not_add_token_limit(self):
        requests = []
        payload = {"model": "command-a-03-2025", "messages": [{"role": "user", "content": "hello"}]}

        def send(client, request):
            requests.append(request)
            return httpx.Response(200, content=b'{"choices":[]}', request=request)

        with patch.object(httpx.Client, "_send_single_request", send):
            response = inference_relay(
                {"model": payload["model"], "base_url": "https://provider.invalid/v1"},
                "private-test-key",
                {"body": payload},
            )
        self.assertEqual(json.loads(requests[0].content), payload)
        self.assertEqual(requests[0].headers["authorization"], "Bearer private-test-key")
        self.assertNotIn("private-test-key", json.dumps(response))
        self.assertEqual(base64.b64decode(response["body_base64"]), b'{"choices":[]}')
        with self.assertRaises(ValueError):
            inference_relay({"model": "different"}, "private-test-key", {"body": payload})

    def test_worker_relay_preserves_sync_and_async_sse_bytes(self):
        sent = []
        body = b'data: {"choices":[]}\n\ndata: [DONE]\n\n'

        class Channel:
            def rpc(self, kind, payload):
                sent.append((kind, payload))
                return {
                    "status": 200,
                    "headers": {
                        "content-type": "text/event-stream",
                        "content-encoding": "gzip",
                        "content-length": "999",
                    },
                    "body_base64": base64.b64encode(body).decode(),
                }

        sync, asynchronous = (
            httpx.Client._send_single_request,
            httpx.AsyncClient._send_single_request,
        )
        try:
            agent_worker.install_model_relay(Channel())
            with httpx.Client() as client:
                response = client.post(
                    "http://model.local/v1/chat/completions", json={"stream": True}
                )
                self.assertEqual(response.content, body)
                self.assertNotIn("content-encoding", response.headers)

            async def exercise():
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        "http://model.local/v1/chat/completions", json={"stream": True}
                    )
                    self.assertEqual(response.content, body)

            asyncio.run(exercise())
            self.assertEqual(sent, [("inference", {"body": {"stream": True}})] * 2)
        finally:
            httpx.Client._send_single_request = sync
            httpx.AsyncClient._send_single_request = asynchronous

    def test_fixture_paths_cannot_escape_and_bytes_are_literal(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(agent_worker, "WORKSPACE", Path(temp).resolve()),
            patch.object(agent_worker, "VISIBLE_WORKSPACE", "/mnt/user-data"),
        ):
            agent_worker.mutate_files(
                [{"type": "write_file", "path": "work/a.txt", "content": "x\ny {WORKSPACE}"}]
            )
            self.assertEqual((Path(temp) / "work/a.txt").read_bytes(), b"x\ny /mnt/user-data")
            snapshot = agent_worker.snapshot()
            self.assertEqual(snapshot["work/a.txt"]["bytes"], 18)
            agent_worker.mutate_files(
                [
                    {
                        "type": "write_file",
                        "path": "{WORKSPACE}/nested/value.txt",
                        "content": "right location",
                    },
                    {
                        "type": "append_file",
                        "path": "{WORKSPACE}\\nested\\value.txt",
                        "content": "\nsecond line",
                    },
                ]
            )
            self.assertEqual(
                (Path(temp) / "nested/value.txt").read_bytes(), b"right location\nsecond line"
            )
            self.assertFalse((Path(temp) / "{WORKSPACE}").exists())
            for name in (
                "../escape",
                str(Path(temp).parent / "escape"),
                ".",
                "{WORKSPACE}/../escape",
            ):
                with self.assertRaises(ValueError):
                    agent_worker.mutate_files(
                        [{"type": "write_file", "path": name, "content": "unsafe"}]
                    )

    def test_protocol_does_not_accept_eof_as_success(self):
        container = object.__new__(AgentContainer)

        class Process:
            stdout = io.StringIO("")

            def poll(self):
                return 1

        container.process = Process()
        with self.assertRaisesRegex(RuntimeError, "container stopped"):
            container._next()


@unittest.skipUnless(
    os.environ.get("NATIVE_AGENT_TEST_IMAGE"), "requires an explicitly selected native Docker image"
)
class NativeContainerTests(unittest.TestCase):
    """Real native tools/history with deterministic inference, never a fake agent."""

    def test_full_reference_preflight_checks_final_artifact_behavior(self):
        agent = os.environ.get("NATIVE_AGENT_TEST_KIND", "openhands")
        requests = []

        def send(client, request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertFalse(body.get("tools"), "Reference must not invoke the task model")
            response = self.reply(
                {"body": body},
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "user": {},
                            "history": {},
                            "newFacts": [],
                            "factsToRemove": [],
                            "factsToReinforce": [],
                        }
                    ),
                },
                len(requests),
            )
            return httpx.Response(
                200,
                content=base64.b64decode(response["body_base64"]),
                headers=response["headers"],
                request=request,
            )

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(httpx.Client, "_send_single_request", send),
        ):
            root = Path(temporary)
            workspace = Workspace(root / "working", Path(__file__).resolve().parents[1])
            for index, source in enumerate(
                ("def greet(name): return 'Hello, ' + name", "def greet(name): return 'WRONG'")
            ):
                action = {
                    "tool": "write_file",
                    "arguments": {"path": "{WORKSPACE}/app.py", "content": source},
                }
                if agent == "openhands":
                    action = {
                        "tool": "file_editor",
                        "arguments": {
                            "command": "create",
                            "path": "{WORKSPACE}/app.py",
                            "file_text": source,
                        },
                    }
                scenario = {
                    "benchmark_version": 2,
                    "id": "greeting",
                    "name": "Greeting",
                    "property_tested": "Behavior",
                    "complexity": {
                        "family": "integrity",
                        "turns": 1,
                        "input_files": 0,
                        "services": 0,
                        "records": 0,
                        "dependency_depth": 1,
                        "fault_types": [],
                        "interaction": "artifact_edit",
                    },
                    "turns": [
                        {
                            "turn_number": 1,
                            "prompt": "Create app.py with greet(name) returning Hello, followed by the name.",
                            "state_checks": [
                                {
                                    "type": "artifact_python",
                                    "name": "greeting behavior",
                                    "python": "from app import greet\nassert greet('Ada') == 'Hello, Ada'",
                                }
                            ],
                            "process_checks": [],
                            "output_checks": [],
                            "reference": {"actions": [action], "final_response": "Created app.py."},
                        }
                    ],
                }
                (workspace.root / "scenarios/greeting.json").write_text(json.dumps(scenario))
                report_path = root / str(index) / "report.json"
                code = native_validation_main(
                    [
                        "--validate-revision",
                        str(workspace.root),
                        "--validation-output",
                        str(report_path),
                        "--agent-kind",
                        agent,
                        "--runtime-image",
                        os.environ["NATIVE_AGENT_TEST_IMAGE"],
                        "--model",
                        "command-a-03-2025",
                        "--base-url",
                        "https://model.invalid/v1",
                    ]
                )
                report = json.loads(report_path.read_text())
                self.assertEqual(code, 0 if index == 0 else 2, report)
                self.assertEqual(report["references"][0]["validated_turns"], 1)
                self.assertTrue(report["references"][0]["full_task_example"])
                if index:
                    failed = report["references"][0]["diagnostics"]["failed_reference_turns"][0]
                    self.assertFalse(failed["artifact_checks"][0]["passed"])
            if agent == "openhands":
                self.assertEqual(requests, [])

    @unittest.skipUnless(
        os.environ.get("NATIVE_AGENT_TEST_KIND") == "deerflow", "DeerFlow memory drain only"
    )
    def test_configured_memory_drain_deadline_is_enforced(self):
        import time

        def inference(payload):
            time.sleep(2)
            return self.reply(
                payload,
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "user": {},
                            "history": {},
                            "newFacts": [],
                            "factsToRemove": [],
                            "factsToReinforce": [],
                        }
                    ),
                },
                1,
            )

        with tempfile.TemporaryDirectory() as temporary:
            for timeout in (1, 8):
                container = AgentContainer(
                    os.environ["NATIVE_AGENT_TEST_IMAGE"],
                    Path(temporary) / str(timeout),
                    inference,
                    lambda _: {"result": {"count": 7}},
                    lambda _: None,
                )
                try:
                    container.start()
                    initialized = container.call(
                        "init",
                        config={
                            "agent_kind": "deerflow",
                            "model": "command-a-03-2025",
                            "memory_flush_timeout": timeout,
                            "tools": [
                                {
                                    "name": "lookup_stock",
                                    "description": "Return current stock.",
                                    "parameters": {"type": "object", "properties": {}},
                                }
                            ],
                        },
                    )
                    self.assertEqual(
                        initialized["runtime_profile"]["memory_flush_timeout_seconds"], timeout
                    )
                    container.call(
                        "reference",
                        prompt="What is the stock count?",
                        actions=[{"tool": "lookup_stock", "arguments": {}}],
                        final_response="The stock count is 7.",
                    )
                except BaseException:
                    container.close()
                    raise
                if timeout == 1:
                    with self.assertRaises(ExceptionGroup) as error:
                        container.close()
                    self.assertIn(
                        "shutdown flush did not complete", str(error.exception.exceptions)
                    )
                else:
                    container.close()

    def test_behavioral_oracle_rejects_swallowed_command_failure(self):
        code = """import os, pathlib, subprocess
root = pathlib.Path(WORKSPACE)
bin_dir = root / 'private_bin'
bin_dir.mkdir()
curl = bin_dir / 'curl'
curl.write_text('#!/bin/sh\\nexit "$HTTP_EXIT"\\n')
curl.chmod(0o755)
env = dict(os.environ, PATH=str(bin_dir) + ':' + os.environ['PATH'])
for exit_code in (0, 22):
    done = subprocess.run(['bash', 'healthcheck.sh'], env=dict(env, HTTP_EXIT=str(exit_code)))
    assert (done.returncode == 0) == (exit_code == 0)
"""
        with tempfile.TemporaryDirectory() as temporary:
            for index, (shell, expected) in enumerate(
                [
                    ("curl -fsS http://localhost:9090/health\n", True),
                    ("curl -fsS http://localhost:9090/health\nexit 0\n", False),
                ]
            ):
                result = evaluate_artifact_check(
                    {"name": "health outcome", "python": code},
                    {"healthcheck.sh": {"kind": "file", "text": shell}},
                    image=os.environ["NATIVE_AGENT_TEST_IMAGE"],
                    workspace="/workspace",
                    visible_workspace="/workspace",
                    output=Path(temporary) / str(index),
                )
                self.assertEqual(result["passed"], expected)

    def test_isolated_artifact_behavior_not_keywords_or_exit_alone(self):
        image = os.environ["NATIVE_AGENT_TEST_IMAGE"]
        with tempfile.TemporaryDirectory() as temp:
            for index, (source, code, expected, timeout) in enumerate(
                [
                    (
                        "def greet(name): return 'Hello, ' + name",
                        "from app import greet\nassert greet('Ada') == 'Hello, Ada'",
                        True,
                        15,
                    ),
                    (
                        "# Hello, Ada\ndef greet(name): return 'WRONG'",
                        "from app import greet\nassert greet('Ada') == 'Hello, Ada'",
                        False,
                        15,
                    ),
                    ("", "import sys; sys.exit(0)", False, 15),
                    ("", "import time; time.sleep(30)", False, 2),
                ]
            ):
                with self.subTest(index=index):
                    snapshot = {"app.py": {"kind": "file", "text": source}}
                    result = evaluate_artifact_check(
                        {"name": "greeting behavior", "python": code, "timeout_seconds": timeout},
                        snapshot,
                        image=image,
                        workspace="/workspace",
                        visible_workspace="/workspace",
                        output=Path(temp) / str(index),
                    )
                    self.assertEqual(result["passed"], expected)
                    self.assertEqual(result["timed_out"], index == 3)
                    self.assertEqual(snapshot["app.py"]["text"], source)
                    self.assertTrue(Path(result["stderr"]).is_file())
                    if index == 1:
                        self.assertIn("AssertionError", Path(result["stderr"]).read_text())

    def test_artifact_verification_is_private_and_does_not_mutate_agent_state(self):
        agent = os.environ.get("NATIVE_AGENT_TEST_KIND", "openhands")
        requests = []

        def send(client, request):
            body = json.loads(request.content)
            requests.append(body)
            message = {"role": "assistant", "content": "Done."}
            if agent == "openhands":
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "finish-1",
                            "type": "function",
                            "function": {
                                "name": "finish",
                                "arguments": json.dumps({"message": "Done."}),
                            },
                        }
                    ],
                }
            elif not body.get("tools"):
                message["content"] = json.dumps(
                    {
                        "user": {},
                        "history": {},
                        "newFacts": [],
                        "factsToRemove": [],
                        "factsToReinforce": [],
                    }
                )
            response = self.reply({"body": body}, message, len(requests))
            return httpx.Response(
                200,
                content=base64.b64decode(response["body_base64"]),
                headers=response["headers"],
                request=request,
            )

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(httpx.Client, "_send_single_request", send),
        ):
            root = Path(temporary)
            report = execute(
                {
                    "scenario": {
                        "id": "behavior_case",
                        "name": "Behavior",
                        "property_tested": "Behavior",
                        "turns": [
                            {
                                "turn_number": 1,
                                "prompt": "Confirm receipt.",
                                "pre_turn_perturbations": [
                                    {"type": "write_file", "path": "app.py", "content": "value = 7"}
                                ],
                                "state_checks": [
                                    {
                                        "type": "artifact_python",
                                        "name": "private_behavior_marker",
                                        "python": "from pathlib import Path\nimport app\nassert app.value == 7\nPath('app.py').write_text('value = 9')\nassert app.value == 8, 'private_behavior_marker'",
                                    }
                                ],
                                "process_checks": [],
                                "output_checks": [],
                            }
                        ],
                    },
                    "output": str(root / "evidence"),
                    "runtime_root": str(root / "runtime"),
                    "runtime_image": os.environ["NATIVE_AGENT_TEST_IMAGE"],
                    "agent_kind": agent,
                    "run_id": "integration",
                    "model": "command-a-03-2025",
                    "base_url": "https://model.invalid/v1",
                }
            )
            self.assertEqual(report["harness_errors"], [])
            self.assertEqual(report["status"], "failed")
            turn = report["turns"][0]
            self.assertEqual(turn["workspace_after_agent"]["app.py"]["text"], "value = 7")
            self.assertFalse(turn["state_check"]["passed"])
            self.assertEqual(len(turn["tool_calls"]), 1 if agent == "openhands" else 0)
            self.assertNotIn("private_behavior_marker", json.dumps(requests))
            self.assertGreater(report["efficiency"]["artifact_verification_seconds"], 0)
            result = turn["artifact_checks"][0]
            self.assertIn("private_behavior_marker", Path(result["stderr"]).read_text())
            summary_path = root / "summary.json"
            summary_path.write_text(
                json.dumps({"cases": [{"id": "behavior_case", "branches": {"ordinary": report}}]})
            )
            audited = audit_campaign(summary_path)
            self.assertTrue(audited["passed"], audited["errors"])
            report["cleanup_warnings"] = ["Native cleanup warning: memory drain incomplete"]
            summary_path.write_text(
                json.dumps({"cases": [{"id": "behavior_case", "branches": {"ordinary": report}}]})
            )
            audited = audit_campaign(summary_path)
            self.assertIn(
                "behavior_case: native cleanup exception downgraded to a non-error phase",
                audited["errors"],
            )
            report.pop("cleanup_warnings")
            summary_path.write_text(
                json.dumps({"cases": [{"id": "behavior_case", "branches": {"ordinary": report}}]})
            )
            Path(result["stderr"]).write_text("edited")
            audited = audit_campaign(summary_path)
            self.assertIn("behavior_case: changed or missing artifact stderr", audited["errors"])

    @staticmethod
    def reply(payload, message, number):
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        body = {
            "id": "stub-" + str(number),
            "object": "chat.completion",
            "created": 0,
            "model": payload["body"]["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }
        if payload["body"].get("stream"):
            delta = dict(message)
            if delta.get("tool_calls"):
                delta["tool_calls"] = [
                    dict(call, index=index) for index, call in enumerate(delta["tool_calls"])
                ]
            chunk = {
                **body,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            raw = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
            content_type = "text/event-stream"
        else:
            raw = json.dumps(body).encode()
            content_type = "application/json"
        return {
            "status": 200,
            "headers": {"content-type": content_type},
            "body_base64": base64.b64encode(raw).decode(),
        }

    def test_mock_reference_checkpoint_and_context_reset(self):
        agent = os.environ.get("NATIVE_AGENT_TEST_KIND", "openhands")
        config = {
            "agent_kind": agent,
            "model": "command-a-03-2025",
            "max_iterations": 4,
            "tools": [
                {
                    "name": "lookup_stock",
                    "description": "Return current stock.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }
        requests, events = [], []

        def inference(payload):
            requests.append(payload["body"])
            message = {"role": "assistant", "content": "The retained count is 7."}
            if agent == "deerflow" and not payload["body"].get("tools"):
                message["content"] = json.dumps(
                    {
                        "user": {},
                        "history": {},
                        "newFacts": [],
                        "factsToRemove": [],
                        "factsToReinforce": [],
                    }
                )
            if agent == "openhands":
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "finish-" + str(len(requests)),
                            "type": "function",
                            "function": {
                                "name": "finish",
                                "arguments": json.dumps({"message": "The retained count is 7."}),
                            },
                        }
                    ],
                }
            return self.reply(payload, message, len(requests))

        with tempfile.TemporaryDirectory() as temp:

            def create(label):
                c = AgentContainer(
                    os.environ["NATIVE_AGENT_TEST_IMAGE"],
                    Path(temp) / label,
                    inference,
                    lambda p: {"result": {"count": 7}},
                    events.append,
                )
                try:
                    c.start()
                    initialized = c.call("init", config=config)
                    names = {tool["function"]["name"] for tool in initialized["tools"]}
                    self.assertIn("lookup_stock", names)
                    if agent == "deerflow":
                        self.assertTrue(
                            {
                                "write_todos",
                                "task",
                                "review_skill_package",
                                "list_uploaded_files",
                                "inspect_run_status",
                            }
                            <= names
                        )
                    else:
                        self.assertTrue({"file_editor", "terminal", "finish"} <= names)
                except Exception:
                    c.close()
                    raise
                return c

            first = create("prefix")
            try:
                first.call(
                    "reference",
                    prompt="What is the stock count?",
                    actions=[{"tool": "lookup_stock", "arguments": {}}],
                    final_response="The stock count is 7.",
                )
                checkpoint = first.call("export")
            finally:
                first.close()
            results = [
                e
                for e in events
                if e.get("event") == "tool_complete" and e["name"] == "lookup_stock"
            ]
            self.assertEqual(len(results), 1)
            self.assertIn("7", json.dumps(results[0]["result"]))
            self.assertFalse(
                any(r.get("tools") for r in requests),
                "authored reference must not invoke the task model",
            )
            # DeerFlow's native memory flush can perform auxiliary inference.
            # These calls must be captured and costed separately in live runs.
            requests.clear()
            second = create("branch")
            try:
                second.call("import", state=checkpoint)
                result = second.call("run", prompt="Remind me of the stock count you just checked.")
                self.assertTrue(result["completed"])
                self.assertEqual(result["final_response"], "The retained count is 7.")
                self.assertIn("The stock count is 7.", json.dumps(requests[0]["messages"]))
                self.assertIn("lookup_stock", json.dumps(requests[0]["messages"]))
                second.call("truncate_history", keep_last_n_messages=0)
                second.call("run", prompt="Hello.")
                self.assertNotIn("The stock count is 7.", json.dumps(requests[-1]["messages"]))
            finally:
                second.close()

    @unittest.skipUnless(
        os.environ.get("NATIVE_AGENT_TEST_KIND") == "deerflow", "DeerFlow native delegation only"
    )
    def test_delegation_captures_child_tools_and_native_acceptance_verdicts(self):
        events, requests = [], []
        target = "/mnt/user-data/workspace/delegated.json"

        def inference(payload):
            body = payload["body"]
            requests.append(body)
            if not body.get("tools"):
                content = json.dumps(
                    {
                        "user": {},
                        "history": {},
                        "newFacts": [],
                        "factsToRemove": [],
                        "factsToReinforce": [],
                    }
                )
                message = {"role": "assistant", "content": content}
            elif not any(
                m.get("role") == "tool" and m.get("tool_call_id") == "child_write_1"
                for m in body["messages"]
            ):
                message = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "child_write_1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {"path": target, "content": "{invalid json"}
                                ),
                            },
                        }
                    ],
                }
            else:
                message = {
                    "role": "assistant",
                    "content": f"Wrote {target} [r1]. The supplied content is not valid JSON.",
                }
            return self.reply(payload, message, len(requests))

        with tempfile.TemporaryDirectory() as temporary:
            container = AgentContainer(
                os.environ["NATIVE_AGENT_TEST_IMAGE"],
                Path(temporary),
                inference,
                lambda p: self.fail("No mock service is involved"),
                events.append,
            )
            try:
                container.start()
                container.call(
                    "init",
                    config={
                        "agent_kind": "deerflow",
                        "model": "command-a-03-2025",
                        "tools": [],
                    },
                )
                result = container.call(
                    "reference",
                    prompt="Create a diagnostic artifact in a delegated task.",
                    actions=[
                        {
                            "tool": "task",
                            "arguments": {
                                "subagent_type": "general-purpose",
                                "prompt": f"Write the literal content {{invalid json to {target}. Report the actual result.",
                                "acceptance_criteria": [
                                    f"file:{target} exists",
                                    f"file:{target} json-valid",
                                ],
                            },
                        }
                    ],
                    final_response="Inspect the native acceptance results.",
                )
                self.assertTrue(result["completed"])
                self.assertEqual(
                    container.call("snapshot")["delegated.json"]["text"], "{invalid json"
                )
            finally:
                container.close()
        observed = [e for e in events if e.get("event") in {"tool_start", "tool_complete"}]
        self.assertEqual(
            [(e["event"], e["name"]) for e in observed],
            [
                ("tool_start", "task"),
                ("tool_start", "write_file"),
                ("tool_complete", "write_file"),
                ("tool_complete", "task"),
            ],
        )
        completed = observed[-1]
        self.assertIn(f"[holds] file:{target} exists", completed["result"])
        self.assertIn(f"[does not hold] file:{target} json-valid", completed["result"])
        self.assertFalse(completed["result"].startswith("Command("))
        self.assertEqual(completed["native_result"]["content"], completed["result"])
        self.assertEqual(completed["native_result"]["tool_call_id"], completed["id"])
        self.assertIn("Command(", completed["native_command"])
        self.assertEqual(sum(bool(r.get("tools")) for r in requests), 2)

    @unittest.skipUnless(
        os.environ.get("NATIVE_AGENT_TEST_KIND") == "deerflow", "DeerFlow native write guard only"
    )
    def test_blocked_native_attempt_is_scored_and_refusal_is_rendered(self):
        requests = []

        def send(client, request):
            body = json.loads(request.content)
            requests.append(body)
            if not body.get("tools"):
                content = json.dumps(
                    {
                        "user": {},
                        "history": {},
                        "newFacts": [],
                        "factsToRemove": [],
                        "factsToReinforce": [],
                    }
                )
                message = {"role": "assistant", "content": content}
            elif any(m.get("tool_call_id") == "blocked-write" for m in body["messages"]):
                message = {"role": "assistant", "content": "The overwrite was blocked."}
            else:
                message = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "blocked-write",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {
                                        "path": "/mnt/user-data/workspace/guarded.txt",
                                        "content": "replacement",
                                    }
                                ),
                            },
                        }
                    ],
                }
            response = self.reply({"body": body}, message, len(requests))
            return httpx.Response(
                200,
                content=base64.b64decode(response["body_base64"]),
                headers=response["headers"],
                request=request,
            )

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(httpx.Client, "_send_single_request", send),
        ):
            root = Path(temporary)
            report = execute(
                {
                    "scenario": {
                        "id": "guarded_case",
                        "name": "Guarded case",
                        "property_tested": "Attempt capture",
                        "turns": [
                            {
                                "turn_number": 1,
                                "prompt": "Update guarded.txt.",
                                "pre_turn_perturbations": [
                                    {
                                        "type": "write_file",
                                        "path": "guarded.txt",
                                        "content": "original",
                                    }
                                ],
                                "state_checks": [
                                    {
                                        "type": "file_contains",
                                        "path": "guarded.txt",
                                        "expected": "original",
                                    }
                                ],
                                "process_checks": [
                                    {"type": "tool_not_called", "tool_name": "write_file"}
                                ],
                                "output_checks": [],
                            }
                        ],
                    },
                    "output": str(root / "evidence"),
                    "runtime_root": str(root / "runtime"),
                    "runtime_image": os.environ["NATIVE_AGENT_TEST_IMAGE"],
                    "agent_kind": "deerflow",
                    "run_id": "integration",
                    "model": "command-a-03-2025",
                    "base_url": "https://model.invalid/v1",
                }
            )
            self.assertEqual(report["harness_errors"], [])
            self.assertEqual(report["status"], "failed")
            turn = report["turns"][0]
            self.assertEqual(turn["workspace_after_agent"]["guarded.txt"]["text"], "original")
            self.assertTrue(turn["state_check"]["passed"])
            self.assertFalse(turn["process_check"]["passed"])
            self.assertEqual(len(turn["tool_calls"]), 1)
            self.assertEqual(turn["execution_events"], [])
            self.assertEqual(turn["tool_call_coverage"]["attempts_without_execution_callback"], 1)
            self.assertEqual(report["efficiency"]["emitted_tool_calls"], 1)
            self.assertEqual(report["efficiency"]["executed_tool_calls"], 0)
            rendered = render_phase(root / "evidence/report.json")
            self.assertEqual(rendered.count("Error: write_file blocked"), 1)
            self.assertLess(
                rendered.index("Error: write_file blocked"),
                rendered.index("The overwrite was blocked."),
            )
            summary_path = root / "summary.json"
            summary = {"cases": [{"id": "guarded_case", "branches": {"ordinary": report}}]}
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            audited = audit_campaign(summary_path)
            self.assertTrue(audited["passed"], audited["errors"])
            # Recomputed cost totals alone would miss a dropped proposal: raw
            # attempt evidence must independently reject the corrupted report.
            turn["tool_calls"] = []
            report["efficiency"] = efficiency(report)
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            audited = audit_campaign(summary_path)
            self.assertTrue(
                any("tool attempt/callback coverage" in error for error in audited["errors"])
            )


if __name__ == "__main__":
    unittest.main()
