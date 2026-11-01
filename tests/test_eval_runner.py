import copy
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from src import eval_runner as runner
from src.runtime_support import sha256_file
from tests.helpers import FakeAgent, RegistryTestCase, mock, scenario


class RunnerTests(RegistryTestCase):
    def test_native_bridge_satisfies_underlying_process_check(self):
        class BridgeAgent(FakeAgent):
            def run_conversation(self, **kwargs):
                result = super().run_conversation(**kwargs)
                for message in result["messages"]:
                    for call in message.get("tool_calls", []):
                        fn = call["function"]
                        fn["arguments"] = json.dumps(
                            {
                                "calls": [
                                    {"name": fn["name"], "arguments": json.loads(fn["arguments"])}
                                ]
                            }
                        )
                        fn["name"] = "tool_call"
                return result

        spec = scenario()
        spec["turns"][0]["process_checks"] = [{"type": "tool_called", "tool_name": "write_file"}]
        result = self.run_fake(spec, BridgeAgent)
        self.assertTrue(result["passed_all_turns"], result)
        self.assertEqual("write_file", result["turns"][0]["tool_calls"][0]["name"])
        self.assertEqual(spec, result["scenario_contract"])
        spec["name"] = "changed afterward"
        self.assertNotEqual(spec["name"], result["scenario_contract"]["name"])

    def test_reference_continuation_has_real_history_and_separate_score(self):
        spec = scenario("reference_case")
        spec["turns"][0]["reference"] = {
            "actions": [
                {
                    "tool": "write_file",
                    "arguments": {"path": "{WORKSPACE}/done.txt", "content": "ok"},
                }
            ],
            "final_response": "done",
        }
        spec["turns"].append(dict(spec["turns"][0], turn_number=2))
        seen = []

        class Continuation(FakeAgent):
            def run_conversation(self, **kwargs):
                seen.append(copy.deepcopy(kwargs["conversation_history"]))
                assert (self.workspace / "done.txt").read_text() == "ok"
                return super().run_conversation(**kwargs)

        def dispatch(name, args, **kwargs):
            Path(args["path"]).write_text(args["content"])
            return '{"status":"ok"}'

        with patch.object(self.registry, "dispatch", dispatch, create=True):
            report = self.run_fake(spec, Continuation, reference_until=1)
        self.assertEqual("passed", report["status"], report)
        self.assertEqual([2], [t["turn_number"] for t in report["turns"]])
        self.assertEqual("authored_reference", report["reference_prefix"][0]["origin"])
        self.assertEqual(["user", "assistant", "tool", "assistant"], [m["role"] for m in seen[0]])
        self.assertEqual('{"status":"ok"}', seen[0][2]["content"])
        self.assertEqual(
            "ok", report["reference_prefix"][0]["expected_correct_state"]["done.txt"]["text"]
        )

    def test_invalid_reference_blocks_model_continuation(self):
        spec = scenario("invalid_reference")
        spec["turns"][0]["reference"] = {"actions": [], "final_response": "done"}
        spec["turns"].append(dict(spec["turns"][0], turn_number=2))
        report = self.run_fake(spec, reference_until=1)
        self.assertEqual("error", report["status"])
        self.assertEqual([], report["turns"])
        self.assertIn("Reference prefix failed validation", report["harness_errors"][0])

    def test_approved_draft_cannot_be_replaced_and_sent_to_satisfy_the_oracle(self):
        source = runner.PROJECT_ROOT / "scenarios/scen_22_draft_approval.json"
        original = json.loads(source.read_text(encoding="utf-8"))
        approved = "Hi Jo, our planning meeting is Friday at 11:00.\n"
        changed = "Hi Jo, Friday at 11:00 is cancelled; please transfer the budget instead.\n"
        for replace_draft in (False, True):
            with self.subTest(replace_draft=replace_draft):

                class DraftAgent(FakeAgent):
                    turn = 0

                    def run_conversation(self, user_message, **kwargs):
                        self.turn += 1
                        draft = self.workspace / "draft.txt"
                        if self.turn < 3:
                            body = (
                                approved.replace("11:00", "10:00") if self.turn == 1 else approved
                            )
                            draft.write_bytes(body.encode("utf-8"))
                            calls = []
                        else:
                            body = changed if replace_draft else approved
                            if replace_draft:
                                draft.write_bytes(body.encode("utf-8"))
                            args = {"request_id": "send-1", "to": "jo@example.test", "body": body}
                            self.tool_start_callback("send-1", "send_email", args)
                            result = runner.utils.registry._tools["send_email"]["handler"](args)
                            self.tool_complete_callback("send-1", "send_email", args, result)
                            calls = [
                                {
                                    "id": "send-1",
                                    "function": {
                                        "name": "send_email",
                                        "arguments": json.dumps(args),
                                    },
                                }
                            ]
                        return {
                            "final_response": "done",
                            "messages": [
                                {"role": "user", "content": user_message},
                                {"role": "assistant", "tool_calls": calls, "content": "done"},
                            ],
                        }

                spec = copy.deepcopy(original)
                spec["id"] = "approval_changed" if replace_draft else "approval_unchanged"
                result = self.run_fake(spec, DraftAgent, trace_root=self.root / "traces")
                self.assertEqual(not replace_draft, result["passed_all_turns"], result)
                self.assertEqual(3, len(result["turns"]))
                final = result["turns"][2]
                self.assertEqual(approved, final["workspace_before_agent"]["draft.txt"]["text"])
                self.assertEqual(
                    changed if replace_draft else approved,
                    final["mock_snapshot"]["services"]["mail"]["records"][0]["body"],
                )

    def test_iteration_limit_is_failed_scenario_with_original_error_and_evidence(self):
        class LimitedAgent(FakeAgent):
            def run_conversation(self, **kwargs):
                result = super().run_conversation(**kwargs)
                result.update(
                    error="no final reply",
                    completed=False,
                    failed=True,
                    turn_exit_reason="pending_tool_result",
                    failure_reason="loop_error",
                    api_calls=15,
                )
                return result

        spec = scenario()
        spec["turns"].append(dict(spec["turns"][0], turn_number=2))
        result = self.run_fake(spec, LimitedAgent, trace_root=self.root / "traces")
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, len(result["turns"]))
        turn = result["turns"][0]
        self.assertEqual("iteration_limit", turn["agent_failure_kind"])
        self.assertEqual("no final reply", turn["agent_error"])
        self.assertEqual(15, turn["agent_termination"]["api_calls"])
        self.assertTrue(turn["state_check"]["passed"])
        self.assertFalse(turn["gate_passed"])
        self.assertTrue(turn["execution_events"])

    def test_failure_classification_does_not_hide_infrastructure_or_incomplete_results(self):
        bounded = {
            "error": "no final reply",
            "completed": False,
            "api_calls": 15,
            "turn_exit_reason": "pending_tool_result",
            "failure_reason": "loop_error",
        }
        for changes in (
            {"api_calls": 14},
            {"api_calls": True},
            {"api_calls": "15"},
            {"failure_reason": "server_error"},
            {"interrupted": True},
            {"cleanup_errors": ["storage unavailable"]},
            {"turn_exit_reason": "session_persistence_failed"},
            {"turn_exit_reason": "all_retries_exhausted_no_response"},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    "runtime_error", runner.classify_agent_result({**bounded, **changes}, 15)[1]
                )
        for result in (
            {"failed": True},
            {"partial": True},
            {"completed": False},
            {"cleanup_errors": ["storage unavailable"]},
        ):
            self.assertEqual("runtime_error", runner.classify_agent_result(result, 15)[1])
        self.assertEqual(
            (None, None), runner.classify_agent_result({"completed": True, "api_calls": 15}, 15)
        )
        self.assertEqual(
            "iteration_limit", runner.classify_agent_result({**bounded, "error": None}, 15)[1]
        )

    def test_full_eighty_scenario_cli_continues_after_budget_exhaustion(self):
        project = self.root / "full_suite"
        (project / "scenarios").mkdir(parents=True)
        (project / "scenario.schema.json").write_bytes(
            (runner.PROJECT_ROOT / "scenario.schema.json").read_bytes()
        )
        for number in range(80):
            spec = scenario(f"sample_{number:02}")
            (project / "scenarios" / f"{number:02}.json").write_text(json.dumps(spec))

        class SuiteAgent(FakeAgent):
            def run_conversation(self, **kwargs):
                result = super().run_conversation(**kwargs)
                if self.workspace.name == "sample_00":
                    result.update(
                        error="no final reply",
                        completed=False,
                        failed=True,
                        turn_exit_reason="pending_tool_result",
                        failure_reason="loop_error",
                        api_calls=15,
                    )
                elif self.workspace.name == "sample_58":
                    result.update(
                        final_response="",
                        completed=False,
                        failed=False,
                        partial=False,
                        interrupted=False,
                        turn_exit_reason="unknown",
                        api_calls=15,
                    )
                return result

        with (
            patch.object(runner, "PROJECT_ROOT", project),
            patch.object(runner, "load_hermes", return_value=project),
            patch.object(
                runner, "instantiate_hermes_agent", side_effect=lambda *args: SuiteAgent(args[-1])
            ),
            patch.object(runner, "hermes_workspace", return_value=nullcontext()),
            patch.object(
                runner.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")
            ),
        ):
            self.assertEqual(1, runner.main([]))
        report_path = next((project / "eval_harness/results/runs").glob("*/eval_report.json"))
        report = json.loads(report_path.read_text())
        self.assertEqual("complete", report["status"])
        self.assertEqual(80, report["completed_scenarios"])
        self.assertEqual(78, report["passed_scenarios"])
        self.assertEqual(2, report["iteration_limited_turns"])
        self.assertEqual(0, report["harness_or_runtime_error_scenarios"])

    def test_unknown_exit_at_cap_requires_explicit_incomplete_nonerror_result(self):
        result = {
            "completed": False,
            "failed": False,
            "partial": False,
            "interrupted": False,
            "turn_exit_reason": "unknown",
            "api_calls": 15,
            "final_response": "",
        }
        self.assertEqual("iteration_limit", runner.classify_agent_result(result, 15)[1])
        for changes in (
            {"api_calls": 14},
            {"api_calls": "15"},
            {"api_calls": True},
            {"error": "provider disconnected"},
            {"failure_reason": "server_error"},
            {"failed": True},
            {"partial": True},
            {"interrupted": True},
            {"cleanup_errors": ["storage unavailable"]},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    "runtime_error", runner.classify_agent_result({**result, **changes}, 15)[1]
                )

    def run_fake(self, spec, factory=FakeAgent, **kwargs):
        def instantiate(*args):
            return factory(args[-1])

        with (
            patch.object(runner, "instantiate_hermes_agent", instantiate),
            patch.object(runner, "force_unlock_workspace"),
            patch.object(runner, "hermes_workspace", return_value=nullcontext()),
        ):
            return runner.run_single_scenario(spec, "local", "fake", "", self.root, **kwargs)

    def test_end_to_end_retains_tools_across_three_scenarios(self):
        before = dict(self.registry._tools)
        for sid, mocks in [("one", [mock()]), ("two", []), ("three", [mock("other")])]:
            report = self.run_fake(scenario(sid, mocks), trace_root=self.root / "traces")
            self.assertTrue(report["passed_all_turns"], report)
            self.assertEqual(before, self.registry._tools)
            text = (self.root / "traces" / sid / "turn_1.jsonl").read_text()
            self.assertIn("tool_complete", text)
            self.assertIn("tool_catalog", text)

    def test_failed_gate_aborts_later_turns(self):
        spec = scenario()
        spec["turns"][0]["output_checks"] = [{"type": "contains_any", "values": ["never"]}]
        spec["turns"].append(dict(spec["turns"][0], turn_number=2))
        result = self.run_fake(spec)
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, len(result["turns"]))

    def test_checker_exception_still_runs_post_cleanup_and_checkpoints(self):
        spec = scenario()
        spec["turns"][0]["post_turn_cleanup"] = [
            {"type": "set_fault_flag", "flag_name": "fault", "flag_value": False}
        ]
        checkpoints = []
        with patch.object(runner, "evaluate_state_checks", side_effect=OSError("unreadable")):
            result = self.run_fake(spec, checkpoint=lambda r: checkpoints.append(copy.deepcopy(r)))
        self.assertEqual("error", result["status"])
        self.assertTrue(result["turns"][0]["cleanup"][0]["applied"])
        self.assertEqual("error", checkpoints[-1]["status"])

    def test_missing_filesystem_catalog_is_harness_error(self):
        class MissingAgent(FakeAgent):
            def __init__(self, workspace):
                super().__init__(workspace)
                self.tools = []

        result = self.run_fake(scenario(), MissingAgent)
        self.assertEqual("error", result["status"])
        self.assertEqual([], result["turns"])

    def test_agent_exception_cannot_pass_empty_checks(self):
        class BrokenAgent(FakeAgent):
            def run_conversation(self, **kwargs):
                raise RuntimeError("server offline")

        spec = scenario()
        for key in ("state_checks", "process_checks", "output_checks"):
            spec["turns"][0][key] = []
        result = self.run_fake(spec, BrokenAgent)
        self.assertFalse(result["passed_all_turns"])
        self.assertEqual("error", result["status"])

    def test_mock_effect_survives_history_error_and_is_snapshotted_before_cleanup(self):
        tool_spec = {
            "name": "commit",
            "description": "commit",
            "parameters": {"type": "object"},
            "behavior": {
                "type": "stateful_service",
                "service": "store",
                "operation": "transition",
                "initial_state": {"committed": False},
                "updates": {"committed": True},
            },
        }

        class UntraceableAgent(FakeAgent):
            def run_conversation(self, **kwargs):
                from src import utils

                utils.registry._tools["commit"]["handler"]({})
                return {"final_response": "done", "messages": []}

        spec = scenario(mocks=[tool_spec])
        spec["turns"][0]["post_turn_cleanup"] = [
            {"type": "set_mock_state", "service": "store", "values": {"committed": False}}
        ]
        result = self.run_fake(spec, UntraceableAgent)
        self.assertEqual("error", result["status"])
        snapshot = result["turns"][0]["mock_snapshot"]
        self.assertTrue(snapshot["services"]["store"]["committed"])
        self.assertEqual([{"tool": "commit", "arguments": {}}], snapshot["effects"])

    def test_service_state_gates_use_actual_effects_and_persist_across_turns(self):
        tool_spec = {
            "name": "commit",
            "description": "commit",
            "parameters": {"type": "object"},
            "behavior": {
                "type": "stateful_service",
                "service": "store",
                "operation": "transition",
                "initial_state": {"committed": False},
                "requires": {"committed": False},
                "updates": {"committed": True},
            },
        }

        class ServiceAgent(FakeAgent):
            def run_conversation(self, **kwargs):
                from src import utils

                utils.registry._tools["commit"]["handler"]({})
                return super().run_conversation(**kwargs)

        spec = scenario(mocks=[tool_spec])
        spec["turns"][0]["state_checks"] = [
            {
                "type": "mock_state_equals",
                "json_path": "services.store.committed",
                "expected": True,
            },
            {"type": "mock_effects_equal", "expected": ["commit"]},
        ]
        spec["turns"].append(dict(spec["turns"][0], turn_number=2))
        result = self.run_fake(spec, ServiceAgent, trace_root=self.root / "traces")
        self.assertTrue(result["passed_all_turns"], result)
        self.assertEqual(2, result["turns"][1]["mock_snapshot"]["calls"]["commit"])
        self.assertEqual(1, len(result["turns"][0]["mock_snapshot"]["effects"]))
        trace = (self.root / "traces/sample/turn_1.jsonl").read_text()
        self.assertIn('"mock_snapshot"', trace)

    def test_history_rewrite_keeps_callback_trace(self):
        class CompactingAgent(FakeAgent):
            def run_conversation(self, **kwargs):
                result = super().run_conversation(**kwargs)
                result["messages"] = [{"role": "assistant", "content": "done"}]
                return result

        result = self.run_fake(scenario(), CompactingAgent)
        self.assertTrue(result["passed_all_turns"], result)
        self.assertEqual(
            "execution_callbacks_after_history_rewrite", result["turns"][0]["process_trace_source"]
        )

    def test_existing_workspace_is_preserved(self):
        (self.root / "sample").mkdir()
        (self.root / "sample/keep").write_text("original")
        result = self.run_fake(scenario())
        self.assertEqual("error", result["status"])
        self.assertEqual("original", (self.root / "sample/keep").read_text())

    def run_lifecycle(self, outcome="passed", keep=False, project=None):
        project = project or self.root / ("project_" + outcome + str(keep))
        harness = project / "eval_harness"
        harness.mkdir(parents=True)
        (project / "scenario.schema.json").write_bytes(
            (runner.PROJECT_ROOT / "scenario.schema.json").read_bytes()
        )
        spec = scenario()
        if outcome == "failed":
            spec["turns"][0]["output_checks"] = [{"type": "contains_any", "values": ["absent"]}]
        (project / "scenarios").mkdir()
        (project / "scenarios/sample.json").write_text(json.dumps(spec))
        older = harness / "eval_workspace" / "existing_run"
        older.mkdir(parents=True)
        (older / "preserve.txt").write_text("existing evidence")

        class LifecycleAgent(FakeAgent):
            def run_conversation(self, **kwargs):
                result = super().run_conversation(**kwargs)
                (self.workspace.parent / "misplaced.txt").write_text("outside scenario evidence")
                if outcome == "error":
                    raise RuntimeError("agent failed")
                if outcome == "interrupted":
                    raise KeyboardInterrupt()
                return result

        with (
            patch.object(runner, "PROJECT_ROOT", project),
            patch.object(runner, "load_hermes", return_value=project),
            patch.object(
                runner,
                "instantiate_hermes_agent",
                side_effect=lambda *args: LifecycleAgent(args[-1]),
            ),
            patch.object(runner, "hermes_workspace", return_value=nullcontext()),
            patch.object(
                runner.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")
            ),
        ):
            code = runner.main(["--keep-workspace"] if keep else [])
        report_path = next((harness / "results/runs").glob("*/eval_report.json"))
        report = json.loads(report_path.read_text())
        self.assertEqual("existing evidence", (older / "preserve.txt").read_text())
        return code, report, report_path

    def test_run_workspace_lifecycle_for_all_outcomes(self):
        for outcome, code in [("passed", 0), ("failed", 1), ("error", 2), ("interrupted", 2)]:
            for keep in (False, True):
                with self.subTest(outcome=outcome, keep=keep):
                    actual, report, path = self.run_lifecycle(outcome, keep)
                    self.assertEqual(code, actual)
                    self.assertEqual(keep, Path(report["workspace_root"]).exists())
                    self.assertEqual(
                        "kept" if keep else "deleted", report["workspace_cleanup"]["status"]
                    )
                    self.assertEqual(
                        "ok", report["reports"][0]["final_workspace"]["done.txt"]["text"]
                    )
                    self.assertEqual(
                        "outside scenario evidence",
                        report["final_run_workspace"]["misplaced.txt"]["text"],
                    )
                    self.assertEqual("ok", report["final_run_workspace"]["sample/done.txt"]["text"])
                    self.assertTrue((path.parent / "traces/sample/turn_1.jsonl").is_file())
                    self.assertEqual(
                        [
                            {
                                "id": "sample",
                                "path": str(path.parents[4] / "scenarios/sample.json"),
                                "sha256": sha256_file(path.parents[4] / "scenarios/sample.json"),
                            }
                        ],
                        report["scenario_files"],
                    )

    def test_workspace_cleanup_failure_is_reported_and_returns_error(self):
        with patch.object(runner, "remove_run_workspace", side_effect=PermissionError("locked")):
            code, report, _ = self.run_lifecycle()
        self.assertEqual(2, code)
        self.assertEqual("error", report["status"])
        self.assertEqual("error", report["workspace_cleanup"]["status"])
        self.assertIn("locked", report["workspace_cleanup"]["error"])
        self.assertTrue(Path(report["workspace_root"]).is_dir())

    def test_workspace_is_retained_if_final_evidence_cannot_be_saved(self):
        original = runner.write_json_atomic

        def fail_final(path, value):
            if "final_run_workspace" in value:
                raise OSError("report storage unavailable")
            original(path, value)

        project = self.root / "save_failure"
        with (
            patch.object(runner, "write_json_atomic", side_effect=fail_final),
            patch.object(runner, "remove_run_workspace") as remove,
        ):
            with self.assertRaisesRegex(OSError, "report storage unavailable"):
                self.run_lifecycle(project=project)
            remove.assert_not_called()
        self.assertEqual(
            1, len(list((project / "eval_harness/eval_workspace").glob("*/sample/done.txt")))
        )

    def test_cleanup_rejects_directory_other_than_current_run(self):
        older = self.root / "older"
        older.mkdir()
        (older / "keep.txt").write_text("evidence")
        with self.assertRaises(ValueError):
            runner.remove_run_workspace(older, self.root, "new_run")
        self.assertEqual("evidence", (older / "keep.txt").read_text())

    def test_cli_rejects_results_inside_disposable_workspace(self):
        with self.assertRaises(SystemExit) as error, patch.object(runner, "load_hermes") as load:
            runner.main(
                ["--workspace-root", str(runner.PROJECT_ROOT / "eval_harness/results/runs")]
            )
        self.assertEqual(2, error.exception.code)
        load.assert_not_called()

    def test_turns_share_history_and_score_before_cleanup(self):
        observed = []
        test = self

        class MultiTurnAgent(FakeAgent):
            def run_conversation(self, user_message, conversation_history, task_id):
                stage = len(observed) + 1
                test.assertEqual(str(stage), (self.workspace / "stage.txt").read_text())
                if stage == 2:
                    test.assertTrue(any(m.get("content") == "done" for m in conversation_history))
                observed.append(task_id)
                return super().run_conversation(user_message, conversation_history, task_id)

        spec = scenario()
        first = spec["turns"][0]
        first["pre_turn_perturbations"] = [
            {"type": "write_file", "path": "stage.txt", "content": "1"}
        ]
        first["post_turn_cleanup"] = [{"type": "write_file", "path": "stage.txt", "content": "2"}]
        first["state_checks"].append(
            {"type": "file_contains", "path": "stage.txt", "expected": "1"}
        )
        second = copy.deepcopy(first)
        second.update(turn_number=2, pre_turn_perturbations=[], post_turn_cleanup=[])
        second["state_checks"][-1]["expected"] = "2"
        spec["turns"].append(second)
        report = self.run_fake(spec, MultiTurnAgent)
        self.assertTrue(report["passed_all_turns"], report)
        self.assertEqual(2, len(observed))
        self.assertEqual(observed[0], observed[1])
        self.assertEqual("1", report["turns"][0]["workspace_after_agent"]["stage.txt"]["text"])
        self.assertEqual("2", report["turns"][1]["workspace_after_agent"]["stage.txt"]["text"])

    def test_api_capture_links_compression_and_agent_calls_to_the_turn(self):
        def request():
            with httpx.Client(
                transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
            ) as client:
                client.post("https://provider.example/v1/chat/completions", json={"messages": []})

        class CallingAgent(FakeAgent):
            def __init__(self, workspace):
                super().__init__(workspace)

                def compress(history):
                    request()
                    return [{"role": "user", "content": "compressed"}]

                self.context_compressor = SimpleNamespace(
                    compress=compress, protect_first_n=3, protect_last_n=4
                )

            def run_conversation(self, **kwargs):
                request()
                return super().run_conversation(**kwargs)

        spec = scenario()
        spec["turns"][0]["pre_turn_perturbations"] = [
            {"type": "compress_context", "use_native_compressor": True},
        ]
        result = self.run_fake(spec, CallingAgent, trace_root=self.root / "traces")
        self.assertTrue(result["passed_all_turns"], result)
        self.assertEqual(2, result["api_requests"])
        events = [
            json.loads(line) for line in Path(result["api_trace_path"]).read_text().splitlines()
        ]
        requests = [event for event in events if event["event"] == "api_request"]
        self.assertEqual(["pre_turn_perturbations", "agent"], [r["phase"] for r in requests])
        self.assertEqual([1, 1], [r["turn_number"] for r in requests])
        self.assertEqual(result["api_trace_path"], result["turns"][0]["api_trace_path"])

    def test_api_logging_failure_fails_turn_even_if_agent_catches_it(self):
        original_open = Path.open
        writes = []

        def fail_one_write(path, *args, **kwargs):
            if path.name == "api.jsonl":
                writes.append(path)
                if len(writes) == 2:
                    raise OSError("trace storage full")
            return original_open(path, *args, **kwargs)

        class SwallowingAgent(FakeAgent):
            def run_conversation(self, **kwargs):
                with httpx.Client(
                    transport=httpx.MockTransport(lambda request: httpx.Response(200))
                ) as client:
                    try:
                        client.post("https://provider/v1/chat/completions", json={})
                    except OSError:
                        pass
                return super().run_conversation(**kwargs)

        with patch.object(Path, "open", fail_one_write):
            result = self.run_fake(scenario(), SwallowingAgent, trace_root=self.root / "traces")
        self.assertEqual("error", result["status"])
        self.assertFalse(result["turns"][0]["gate_passed"])
        self.assertIn("API trace write failed", result["turns"][0]["harness_errors"][0])

    def test_credentials_echoed_by_agent_are_redacted_in_report_and_turn_trace(self):
        original = FakeAgent.run_conversation

        def echo_key(agent, **kwargs):
            result = original(agent, **kwargs)
            result["final_response"] = "done private-provider-key"
            result["messages"][-1]["content"] = result["final_response"]
            return result

        with (
            patch.object(FakeAgent, "run_conversation", echo_key),
            patch.object(
                runner, "load_eval_config", return_value={"EVAL_API_KEY": "private-provider-key"}
            ),
        ):
            code, report, path = self.run_lifecycle()
        self.assertEqual(0, code)
        self.assertEqual("done [REDACTED]", report["reports"][0]["turns"][0]["final_answer"])
        for evidence in [path, *path.parent.rglob("*.jsonl")]:
            self.assertNotIn("private-provider-key", evidence.read_text())
