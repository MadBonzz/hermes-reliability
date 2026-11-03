"""Offline orchestration contracts; no provider or installed Hermes required."""

import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from src.evaluation.api_trace import SecretRedactor
from src.evaluation.runtime_support import sha256_file
from src.orchestrator.adapters import ContainerAdapter, HermesAdapter, make_adapter, revision_hashes
from src.orchestrator.container_worker import main as native_validation_main
from src.orchestrator.container_worker import reference_diagnostics
from src.orchestrator.quality import validate_analysis, validate_review
from src.orchestrator.runner import (
    Client,
    Evidence,
    exchange,
    initial_recovery,
    review_recovery,
    run,
    target_api_usage,
    validation_evidence,
)
from src.orchestrator.transcripts import render_phase
from src.orchestrator.workspace import Workspace

ROOT = Path(__file__).resolve().parent.parent


def case():
    return {
        "benchmark_version": 2,
        "id": "task_a",
        "name": "A",
        "property_tested": "integrity",
        "complexity": {
            "family": "integrity",
            "turns": 1,
            "input_files": 0,
            "services": 0,
            "records": 0,
            "dependency_depth": 1,
            "fault_types": [],
            "interaction": "answer",
        },
        "turns": [
            {
                "turn_number": 1,
                "prompt": "What is two plus two?",
                "state_checks": [],
                "process_checks": [],
                "output_checks": [{"type": "contains_all", "values": ["4"]}],
            }
        ],
    }


class OrchestratorTests(unittest.TestCase):
    def test_adapter_factory_uses_current_schema_and_rejects_retired_command_protocol(self):
        adapter = make_adapter({"kind": "hermes"})
        self.assertIsInstance(adapter, HermesAdapter)
        self.assertEqual(adapter.schema_path.name, "scenario.schema.json")
        with self.assertRaisesRegex(ValueError, "Unknown adapter kind"):
            make_adapter({"kind": "command"})

    def test_retired_recovery_options_fail_before_creating_campaign(self):
        with patch("src.orchestrator.runner.Path.mkdir") as mkdir:
            for option in ("resume_final_execution_from", "recovery_evaluator_repairs"):
                with self.subTest(option=option), self.assertRaisesRegex(ValueError, "retired"):
                    run({option: "old-campaign"})
            mkdir.assert_not_called()

    def test_campaign_target_usage_includes_preflight_and_deduplicates_copies(self):
        def events(rid, phase, incoming=None, outgoing=None):
            rows = [
                {
                    "event": "api_request",
                    "request_id": rid,
                    "phase": phase,
                    "body": {"model": "target"},
                }
            ]
            if incoming is not None:
                rows.append(
                    {
                        "event": "api_response_body",
                        "request_id": rid,
                        "body": {
                            "usage": {"prompt_tokens": incoming, "completion_tokens": outgoing},
                            "choices": [],
                        },
                    }
                )
            return rows

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = {
                "round_001/generation_01/validation/a/api.jsonl": events("a", "shutdown", 3, 5),
                "round_001/generation_01/validation/copy/api.jsonl": events("a", "shutdown", 3, 5),
                "round_001/candidate/preflight/a/api.jsonl": events("b", "shutdown", 7, 11),
                "round_001/candidate/execution/a/api.jsonl": events("c", "agent", 13, 17)
                + events("d", "agent"),
            }
            for name, rows in fixtures.items():
                path = root / name
                path.parent.mkdir(parents=True)
                path.write_text("\n".join(json.dumps(row) for row in rows))
            result = target_api_usage(root)
            self.assertEqual(result["totals"]["api_requests"], 4)
            self.assertEqual(result["totals"]["reported_tokens"]["input_tokens"], 23)
            self.assertEqual(result["totals"]["reported_tokens"]["output_tokens"], 33)
            self.assertFalse(result["totals"]["usage_complete"])
            self.assertEqual(result["by_stage"]["generation_validation"]["api_requests"], 1)
            self.assertEqual(result["by_stage"]["candidate_preflight"]["api_requests"], 1)
            self.assertEqual(result["by_runtime_phase"]["shutdown"]["api_requests"], 2)
            self.assertEqual(result["totals"]["usage_missing_request_ids"], ["d"])
            self.assertEqual(result["errors"], [])
            (root / "broken").mkdir()
            (root / "broken/api.jsonl").write_text('{"event":')
            self.assertEqual(len(target_api_usage(root)["errors"]), 1)

    def test_native_preflight_can_validate_optional_final_reference(self):
        for count, final_reference in ((1, False), (1, True), (2, False), (2, True)):
            with (
                self.subTest(count=count, final_reference=final_reference),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                workspace = Workspace(root / "working", ROOT)
                scenario = case()
                scenario["turns"] = [
                    json.loads(json.dumps(scenario["turns"][0])) for _ in range(count)
                ]
                scenario["complexity"]["turns"] = count
                for i, turn in enumerate(scenario["turns"], 1):
                    turn["turn_number"] = i
                    if i < count or final_reference:
                        turn["reference"] = {"actions": [], "final_response": "4"}
                workspace.action(
                    {
                        "op": "write",
                        "path": "scenarios/task_a.json",
                        "content": json.dumps(scenario),
                    }
                )
                report_path = root / "validation/report.json"
                with patch(
                    "src.orchestrator.container_worker.execute",
                    return_value={
                        "status": "passed",
                        "harness_errors": [],
                        "efficiency": {},
                        "reference_prefix": [],
                    },
                ) as execute:
                    code = native_validation_main(
                        [
                            "--validate-revision",
                            str(workspace.root),
                            "--validation-output",
                            str(report_path),
                            "--agent-kind",
                            "deerflow",
                            "--runtime-image",
                            "stub",
                            "--model",
                            "model",
                            "--base-url",
                            "https://model.invalid/v1",
                            "--memory-flush-timeout",
                            "120",
                        ]
                    )
                    self.assertEqual(code, 0, report_path.read_text())
                    result = json.loads(report_path.read_text())
                    expected = count if final_reference else count - 1
                    if expected:
                        config = execute.call_args.args[0]
                        self.assertEqual(config["reference_until"], expected)
                        self.assertEqual(config["memory_flush_timeout"], 120)
                        self.assertEqual(len(config["scenario"]["turns"]), expected)
                        self.assertEqual(
                            result["references"][0]["full_task_example"], final_reference
                        )
                    else:
                        execute.assert_not_called()
                        self.assertEqual(result["references"], [])

    def test_memory_deadline_is_forwarded_to_preflight_and_execution(self):
        adapter = ContainerAdapter.__new__(ContainerAdapter)
        adapter.config = {
            "kind": "deerflow",
            "python": "python",
            "runtime_image": "pinned",
            "memory_flush_timeout": 120,
            "target": {"model": "target", "base_url": "https://model.invalid/v1"},
        }
        for command in (
            adapter.validation_command(Path("evaluator"), Path("output")),
            adapter.execution_command(Path("evaluator"), Path("output"), adapter.config["target"]),
        ):
            self.assertEqual(command[command.index("--memory-flush-timeout") + 1], "120")

    def test_host_probe_does_not_execute_artifact_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Workspace(Path(temporary) / "working", ROOT)
            scenario = case()
            scenario["turns"][0]["state_checks"] = [
                {
                    "type": "artifact_python",
                    "name": "private",
                    "python": "raise RuntimeError('executed')",
                }
            ]
            (workspace.root / "scenarios/task_a.json").write_text(json.dumps(scenario))
            with self.assertRaisesRegex(ValueError, "isolated Docker verification"):
                workspace.probe(
                    {
                        "path": "scenarios/task_a.json",
                        "turn_number": 1,
                        "expect_pass": False,
                        "reason": "behavior check",
                    }
                )

    def test_modified_checker_cannot_receive_unsupported_review_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "working", ROOT)
            checker = workspace.root / "src/evaluation/checks.py"
            original = checker.read_text(encoding="utf-8")
            accepted = {"accepted": True, "issues": []}
            checker.write_text(original + "\n# formatting-only comment\n", encoding="utf-8")
            evidence = workspace.oracle_validation(ROOT, {"test_files": [], "tests_exit_code": 0})
            self.assertFalse(evidence["checker_code_changed"])
            validate_review(accepted, "verification_attribution", [], evidence)
            checker.write_text(original + "\nORACLE_OVERRIDE = True\n", encoding="utf-8")
            evidence = workspace.oracle_validation(ROOT, {"test_files": [], "tests_exit_code": 0})
            self.assertTrue(evidence["checker_code_changed"])
            with self.assertRaisesRegex(ValueError, "passing executable"):
                validate_review(accepted, "verification_attribution", [], evidence)
            rejected = {
                "accepted": False,
                "issues": [
                    {
                        "id": "verification_attribution-missing-tests",
                        "description": "No executable counterexamples.",
                    }
                ],
            }
            validate_review(rejected, "verification_attribution", [], evidence)
            failed = workspace.oracle_validation(
                ROOT, {"test_files": ["test_oracle.py"], "tests_exit_code": 1}
            )
            with self.assertRaises(ValueError):
                validate_review(accepted, "verification_attribution", [], failed)
            passed = workspace.oracle_validation(
                ROOT, {"test_files": ["test_oracle.py"], "tests_exit_code": 0}
            )
            validate_review(accepted, "verification_attribution", [], passed)

    def test_native_catalog_inspection_disables_dispatch_and_respects_frozen_bridge(self):
        adapter = ContainerAdapter.__new__(ContainerAdapter)
        adapter.config = {
            "kind": "openhands",
            "runtime_image": "pinned",
            "source_revision": "revision",
            "target": {"model": "target"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch("src.evaluation.agent_container.AgentContainer") as factory:
                runtime = factory.return_value
                runtime.call.return_value = {"tools": [{"function": {"name": "file_editor"}}]}
                result = adapter.inspect_tool_catalog(directory / "catalog", ROOT)
                self.assertTrue(result["available"])
                runtime.call.assert_called_once()
                self.assertEqual(runtime.call.call_args.args, ("init",))
                self.assertEqual(runtime.call.call_args.kwargs["config"]["tools"], [])
                runtime.close.assert_called_once()
                for dispatch in factory.call_args.args[2:4]:
                    with self.assertRaisesRegex(RuntimeError, "must not invoke"):
                        dispatch({})
                runtime.reset_mock()
                runtime.call.side_effect = RuntimeError("native import failed")
                with self.assertRaisesRegex(RuntimeError, "native import"):
                    adapter.inspect_tool_catalog(directory / "failed", ROOT)
                runtime.close.assert_called_once()
            (directory / "frozen/src/evaluation").mkdir(parents=True)
            (directory / "frozen/src/evaluation/agent_worker.py").write_text("# older bridge")
            with patch("src.evaluation.agent_container.AgentContainer") as factory:
                result = adapter.inspect_tool_catalog(directory / "old", directory / "frozen")
                self.assertFalse(result["available"])
                factory.assert_not_called()

    def test_native_reference_feedback_retains_actual_failed_calls(self):
        call = {
            "id": "bad",
            "name": "write_file",
            "arguments": {"path": "/mnt/user-data/out.txt"},
            "result": "Permission denied: outside permitted workspace",
            "completed": True,
        }
        report = {
            "workspace": "/mnt/user-data",
            "reference_prefix": [
                {"turn_number": 1, "gate_passed": True},
                {
                    "turn_number": 2,
                    "gate_passed": False,
                    "execution_events": [call],
                    "state_check": {"passed": False, "details": ["missing output"]},
                },
            ],
        }
        result = reference_diagnostics(report)
        self.assertEqual(result["workspace"], "/mnt/user-data")
        self.assertEqual(len(result["failed_reference_turns"]), 1)
        self.assertEqual(result["failed_reference_turns"][0]["calls"][0]["result"], call["result"])
        self.assertEqual(
            result["failed_reference_turns"][0]["checks"]["state"],
            report["reference_prefix"][1]["state_check"],
        )

    def test_reference_feedback_exposes_command_failure_even_when_gate_passed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "turn_1.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "event": "tool_complete",
                        "id": "c1",
                        "name": "terminal",
                        "result": "No module named pytest",
                        "native_result": {"observation": {"is_error": False, "exit_code": 1}},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            report = {
                "reference_prefix": [
                    {"turn_number": 1, "gate_passed": True, "trace_path": str(path)}
                ]
            }
            result = reference_diagnostics(report)
            self.assertEqual(result["failed_reference_turns"], [])
            self.assertEqual(result["reported_tool_failures"][0]["native_status"]["exit_code"], 1)
            self.assertTrue(result["reported_tool_failures"][0]["gate_passed"])

    def test_reviewer_oracle_probe_exposes_false_positive_without_executing_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = Workspace(root / "working", ROOT)
            spec = case()
            spec["turns"][0].update(
                state_checks=[
                    {"type": "file_contains", "path": "module.py", "expected": "exported_function"},
                    {"type": "python_syntax", "path": "module.py"},
                ],
                output_checks=[],
            )
            scenario = workspace.root / "scenarios/task_a.json"
            scenario.write_text(json.dumps(spec), encoding="utf-8")
            before = workspace.inventory()
            probe = {
                "op": "probe",
                "path": "scenarios/task_a.json",
                "turn_number": 1,
                "expect_pass": False,
                "reason": "A comment does not define a callable export",
                "files": {
                    "module.py": "# exported_function\nraise RuntimeError('Must never run')\n"
                },
            }
            result = workspace.action(probe, readonly=True)
            self.assertTrue(result["passed"])
            self.assertFalse(result["matches_expectation"])
            self.assertTrue(result["candidate_checker_matches"])
            self.assertEqual(before, workspace.inventory())
            probe["files"]["module.py"] = "# unrelated comment\n"
            result = workspace.action(probe, readonly=True)
            self.assertFalse(result["passed"])
            self.assertTrue(result["matches_expectation"])

    def test_oracle_probe_conditions_and_path_containment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = Workspace(root / "working", ROOT)
            spec = case()
            spec["experiment"] = {
                "branch_turn": 1,
                "baseline_turns": [
                    {"turn_number": 1, "output_checks": [{"type": "contains_all", "values": ["5"]}]}
                ],
            }
            scenario = workspace.root / "scenarios/task_a.json"
            scenario.write_text(json.dumps(spec), encoding="utf-8")
            probe = {
                "op": "probe",
                "path": "scenarios/task_a.json",
                "turn_number": 1,
                "condition": "baseline",
                "expect_pass": True,
                "reason": "Matches baseline oracle",
                "final_response": "5",
            }
            self.assertTrue(workspace.action(probe, readonly=True)["matches_expectation"])
            self.assertFalse(
                workspace.action({**probe, "condition": "treatment"}, readonly=True)[
                    "matches_expectation"
                ]
            )
            with self.assertRaisesRegex(ValueError, "condition"):
                workspace.action({**probe, "condition": "ordinary"}, readonly=True)
            for name in ("../escape.txt", str(root / "outside.txt")):
                with self.assertRaisesRegex(ValueError, "escapes"):
                    workspace.action({**probe, "files": {name: "bad"}}, readonly=True)
            spec["turns"][0]["state_checks"] = [
                {"type": "file_contains", "path": str(root / "outside.txt"), "expected": "private"}
            ]
            scenario.write_text(json.dumps(spec), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "escapes"):
                workspace.action(probe, readonly=True)
            self.assertFalse((root / "outside.txt").exists())

    def test_preflight_failures_expose_checks_and_readable_raw_evidence_to_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            report = {
                "id": "case",
                "scenario_contract": {},
                "reference_prefix": [
                    {
                        "turn_number": 2,
                        "state_check": {
                            "passed": False,
                            "details": ["[FAIL] expected updated file"],
                        },
                        "process_check": {"passed": True, "details": []},
                    }
                ],
            }
            original = json.dumps(report)
            (directory / "report.json").write_text(original, encoding="utf-8")
            (directory / "turn_2.jsonl").write_text(
                '{"event":"tool_complete","result":"Cannot overwrite using create"}\n',
                encoding="utf-8",
            )
            evidence = Evidence(SecretRedactor())
            result = validation_evidence({"passed": False}, directory, evidence)
            self.assertFalse(result["passed"])
            self.assertEqual(result["reference_failures"][0]["turn_number"], 2)
            self.assertEqual(set(result["reference_failures"][0]["failed_checks"]), {"state"})
            trace_id = next(
                eid
                for eid, item in result["evidence"].items()
                if item["relative_path"] == "turn_2.jsonl"
            )
            self.assertIn("Cannot overwrite", evidence.read({"id": trace_id})["text"])
            self.assertEqual((directory / "report.json").read_text(encoding="utf-8"), original)

    def test_invalid_drafts_remain_visible_for_review_without_becoming_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "working", ROOT)
            invalid = case()
            invalid["turns"][0]["reference"] = {
                "actions": [{"tool": "nonexistent_native_tool", "arguments": {}}],
                "final_response": "done",
            }
            (workspace.root / "scenarios/task_a.json").write_text(json.dumps(invalid))
            (workspace.root / "scenarios/broken.json").write_text("not JSON")
            review = workspace.review_scenarios()
            self.assertEqual([s["id"] for s in review["scenarios"]], ["task_a"])
            self.assertEqual(review["errors"][0]["path"], "scenarios/broken.json")
            with self.assertRaises(ValueError):
                workspace.validate(5)

    def test_partial_read_batch_keeps_successful_source_and_evidence_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "native"
            source.mkdir()
            (source / "runtime.py").write_text("native implementation", encoding="utf-8")
            evidence = Evidence(SecretRedactor())
            eid = evidence.add(source / "runtime.py")
            answers = iter(
                [
                    {
                        "source": [
                            {"path": "runtime.py"},
                            {"path": "missing.py"},
                            {"path": "../outside.py"},
                        ]
                    },
                    {"read": [{"id": "missing"}, {"id": eid}]},
                    {"ready": True},
                ]
            )

            class Reader:
                def ask(self, messages):
                    return next(answers)

            conversation = []
            exchange(Reader(), evidence, {}, source=source, conversation=conversation)
            source_results = json.loads(conversation[3]["content"])
            self.assertEqual(source_results[0]["text"], "native implementation")
            self.assertIn(source_results[0]["ref"], evidence.seen)
            self.assertIn("source_index", source_results[1]["hint"])
            self.assertIn("outside allowed", source_results[2]["error"])
            evidence_results = json.loads(conversation[5]["content"])
            self.assertIn("error", evidence_results[0])
            self.assertEqual(evidence_results[1]["text"], "native implementation")

    def test_workspace_rejects_nested_source_instead_of_silently_ignoring_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "working", ROOT)
            with self.assertRaisesRegex(ValueError, "top-level"):
                workspace.action({"op": "list", "source": [{"path": "runtime.py"}]})

    def test_repeated_reads_add_advice_without_blocking_reads_or_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "working", ROOT)
            path = workspace.root / "scenarios/task_a.json"
            path.write_text('{"id":"old"}', encoding="utf-8")
            evidence = Evidence(SecretRedactor())
            read = {"op": "read", "path": "scenarios/task_a.json", "length": 4000}
            answers = iter(
                [
                    {"actions": [read]},
                    {"actions": [{**read, "length": 16000}]},
                    {"actions": [read]},
                    {
                        "actions": [
                            {
                                "op": "replace",
                                "path": read["path"],
                                "expected_sha256": sha256_file(path),
                                "old": '"old"',
                                "new": '"new"',
                            }
                        ]
                    },
                    {"actions": [read]},
                    {"ready": True},
                ]
            )

            class Reader:
                def ask(self, messages):
                    return next(answers)

            conversation = []
            journal = Path(tmp) / "actions.jsonl"
            exchange(
                Reader(),
                evidence,
                {},
                workspace=workspace,
                conversation=conversation,
                journal=journal,
            )
            rows = [json.loads(line) for line in journal.read_text().splitlines()]
            self.assertNotIn("repeated_read", rows[1]["results"][0])
            repeated = rows[2]["results"][0]
            self.assertEqual(repeated["repeated_read"]["count"], 3)
            self.assertEqual(repeated["result"]["text"], '{"id":"old"}')
            self.assertIn("repeated_read", json.loads(conversation[7]["content"])[0])
            self.assertEqual(rows[4]["results"][0]["result"]["text"], '{"id":"new"}')
            self.assertNotIn("repeated_read", rows[4]["results"][0])

    def test_interrupted_review_recovery_keeps_round_budget_and_saved_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prior = root / "eval_harness/orchestrator/prior"
            prior.mkdir(parents=True)
            working = Workspace(prior / "working", ROOT)
            Workspace(prior / "trusted", ROOT)
            working.action(
                {"op": "write", "path": "scenarios/task_a.json", "content": json.dumps(case())}
            )
            stage = prior / "round_003/generation_01"
            stage.mkdir(parents=True)
            working.snapshot(stage / "candidate")
            config = {
                "scope": "test",
                "source": {},
                "target": {},
                "adapter": {},
                "limits": {"rounds": 5, "generations": 3},
                "orchestrator": {},
            }
            (prior / "config.json").write_text(json.dumps(config))
            (prior / "campaign.json").write_text(
                json.dumps(
                    {
                        "stop_reason": "error",
                        "rounds": [
                            {"round": 1, "executions": {"candidate": {"status": "complete"}}},
                            {"round": 2, "executions": {"candidate": {"status": "error"}}},
                            {"round": 3, "generations": [], "executions": {}},
                        ],
                    }
                )
            )
            source = prior / "source.py"
            source.write_text("# source")
            evidence = Evidence(SecretRedactor())
            eid = evidence.add(source)
            chunk = evidence.read({"id": eid})
            plan = {
                "ready": True,
                "rationale": [{"id": "task_a", "architecture_refs": [chunk["ref"]]}],
            }
            (stage / "proposal.json").write_text(json.dumps(plan))
            (stage / "validation.json").write_text('{"passed":true}')
            (stage / "review_task_quality.json").write_text(
                '{"accepted":false,"issues":[{"description":"needs depth"}]}'
            )
            messages = [
                {"role": "system", "content": "policy"},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"task": "Generate tests", "round": 3, "source_index": eid}
                            ),
                        }
                    ],
                },
                {"role": "assistant", "content": "read"},
                {"role": "user", "content": json.dumps([chunk])},
            ]
            (prior / "api_0001.request.json").write_text(json.dumps({"messages": messages}))
            (prior / "evidence_index.json").write_text(json.dumps(evidence.items))
            config["resume_from"] = str(prior)
            with patch("src.orchestrator.runner.ROOT", root):
                recovered = review_recovery(config, Evidence(SecretRedactor()))
            self.assertEqual(3, recovered["round"])
            self.assertEqual(2, recovered["generation"])
            self.assertEqual(plan, json.loads(recovered["messages"][-1]["content"]))
            self.assertEqual(1, len(recovered["state"]["rounds"][-1]["generations"]))
            copied = Workspace.clone(root / "copied", working.root)
            self.assertEqual(
                (copied.root / "scenarios/task_a.json").read_bytes(),
                (working.root / "scenarios/task_a.json").read_bytes(),
            )
            state_path = prior / "campaign.json"
            stopped = json.loads(state_path.read_text())
            stopped.update(
                stop_reason="operator_preempted",
                preemption_reason="Computed answers were exposed in user requests.",
            )
            state_path.write_text(json.dumps(stopped))
            with patch("src.orchestrator.runner.ROOT", root):
                recovered = review_recovery(config, Evidence(SecretRedactor()))
            self.assertEqual(recovered["generation"], 2)
            self.assertEqual(recovered["round"], 3)
            self.assertEqual(recovered["prior_stop_reason"], "operator_preempted")
            self.assertEqual(recovered["state"]["stop_reason"], "error")
            self.assertNotIn("preemption_reason", recovered["state"])
            self.assertIn("Computed answers", recovered["feedback"][-1]["description"])
            self.assertNotEqual(recovered["messages"][0]["content"], "policy")
            stopped["rounds"][-1]["executions"] = {"candidate": {"status": "error"}}
            state_path.write_text(json.dumps(stopped))
            with (
                patch("src.orchestrator.runner.ROOT", root),
                self.assertRaisesRegex(ValueError, "cannot resume an execution"),
            ):
                review_recovery(config, Evidence(SecretRedactor()))
            stopped["rounds"][-1]["executions"] = {}
            state_path.write_text(json.dumps(stopped))
            (working.root / "src/evaluation/new.py").write_text("changed=True")
            with (
                patch("src.orchestrator.runner.ROOT", root),
                self.assertRaisesRegex(ValueError, "Workspace changed"),
            ):
                review_recovery(config, Evidence(SecretRedactor()))

    def test_transient_api_retry_keeps_request_and_separate_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            req = httpx.Request("POST", "http://local/v1/chat/completions")
            responses = [
                httpx.Response(
                    503, headers={"Retry-After": "60"}, json={"error": "overloaded"}, request=req
                ),
                httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"finish_reason": "stop", "message": {"content": '{"ready":true}'}}
                        ]
                    },
                    request=req,
                ),
            ]
            client = Client(
                {"model": "planner", "base_url": "http://local/v1"},
                root,
                SecretRedactor(),
                "key",
                None,
            )
            messages = [{"role": "user", "content": "generate"}]
            with (
                patch("httpx.Client.post", side_effect=responses) as post,
                patch("src.orchestrator.runner.time.sleep") as sleep,
            ):
                self.assertEqual({"ready": True}, client.ask(messages))
                self.assertEqual(
                    post.call_args_list[0].kwargs["json"], post.call_args_list[1].kwargs["json"]
                )
                self.assertEqual(60, sum(c.args[0] for c in sleep.call_args_list))
            self.assertEqual(2, client.calls)
            self.assertEqual(
                503, json.loads((root / "api_0001.response.json").read_text())["status"]
            )
            self.assertEqual(
                200, json.loads((root / "api_0002.response.json").read_text())["status"]
            )
            self.assertFalse(
                json.loads((root / "api_0001.retry.json").read_text())["local_actions_replayed"]
            )
            with patch("httpx.Client.post", return_value=httpx.Response(401, request=req)) as post:
                with self.assertRaises(httpx.HTTPStatusError):
                    client.ask(messages)
                self.assertEqual(1, post.call_count)

    def test_initial_timeout_recovery_preserves_context_and_rejects_workspace_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prior = root / "eval_harness/orchestrator/old"
            prior.mkdir(parents=True)
            Workspace(prior / "working", ROOT)
            Workspace(prior / "trusted", ROOT)
            config = {
                "scope": "test",
                "source": {},
                "target": {},
                "adapter": {},
                "limits": {},
                "orchestrator": {"model": "planner", "timeout_seconds": 180},
            }
            (prior / "config.json").write_text(json.dumps(config))
            (prior / "campaign.json").write_text(
                json.dumps(
                    {"stop_reason": "error", "rounds": [{"generations": [], "executions": {}}]}
                )
            )
            source = prior / "agent_source.json"
            source.write_text("{}")
            original = Evidence(SecretRedactor())
            eid = original.add(source)
            chunk = original.read({"id": eid})
            messages = [
                {"role": "system", "content": "policy"},
                {
                    "role": "user",
                    "content": [{"type": "text", "text": json.dumps({"source_index": eid})}],
                },
                {"role": "user", "content": json.dumps([chunk])},
            ]
            (prior / "api_0001.request.json").write_text(json.dumps({"messages": messages}))
            (prior / "api_0001.error.json").write_text('{"type":"ReadTimeout"}')
            (prior / "evidence_index.json").write_text(json.dumps(original.items))
            config["resume_initial_from"] = str(prior)
            config["orchestrator"]["read_timeout_seconds"] = None
            restored = Evidence(SecretRedactor())
            with patch("src.orchestrator.runner.ROOT", root):
                recovery = initial_recovery(config, restored)
                self.assertEqual(recovery["messages"], messages)
                self.assertIn(chunk["ref"], restored.seen)

                class PendingClient:
                    def ask(inner, actual):
                        self.assertEqual(actual, messages)
                        return {"ready": True}

                exchange(
                    PendingClient(),
                    restored,
                    {"should_not_be_appended": True},
                    conversation=json.loads(json.dumps(messages)),
                    continue_pending=True,
                )
                (prior / "working/src/evaluation/changed.py").write_text("changed = True")
                with self.assertRaisesRegex(ValueError, "unmodified initial workspace"):
                    initial_recovery(config, restored)

    def test_invalid_json_edit_preserves_last_valid_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "working", ROOT)
            name = "scenarios/task_a.json"
            with self.assertRaises(json.JSONDecodeError):
                workspace.action({"op": "write", "path": name, "content": '{"id":'})
            self.assertFalse(workspace.path(name).exists())
            content = json.dumps(case())
            result = workspace.action({"op": "write", "path": name, "content": content})
            with self.assertRaises(json.JSONDecodeError):
                workspace.action(
                    {
                        "op": "replace",
                        "path": name,
                        "expected_sha256": result["sha256"],
                        "old": content,
                        "new": content[:-1],
                    }
                )
            self.assertEqual(workspace.path(name).read_text(encoding="utf-8"), content)
            self.assertEqual(sha256_file(workspace.path(name)), result["sha256"])

    def test_workspace_packages_and_hashes_cover_evaluation_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "working", ROOT)
            self.assertTrue((workspace.root / "src/__init__.py").is_file())
            self.assertTrue((workspace.root / "src/evaluation/evaluate.py").is_file())
            self.assertFalse((workspace.root / "src/orchestrator").exists())
            before = revision_hashes(workspace.root)
            workspace.action(
                {"op": "write", "path": "src/evaluation/custom.py", "content": "VALUE = 1"}
            )
            after = revision_hashes(workspace.root)
            self.assertNotEqual(before, after)
            self.assertIn("src/evaluation/custom.py", workspace.evaluator_changes(ROOT))
            for name in ("src/orchestrator/prompts.py", "src/custom.py", "src/__init__.py"):
                with self.subTest(path=name), self.assertRaises(ValueError):
                    workspace.action({"op": "write", "path": name, "content": "bad"})

    def test_workspace_edits_are_scoped_versioned_and_reviewers_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "working", ROOT)
            result = workspace.action(
                {"op": "write", "path": "scenarios/task_a.json", "content": json.dumps(case())}
            )
            with self.assertRaises(ValueError):
                workspace.action(
                    {
                        "op": "delete",
                        "path": "scenarios/task_a.json",
                        "expected_sha256": result["sha256"],
                    },
                    readonly=True,
                )
            with self.assertRaises(ValueError):
                workspace.action(
                    {
                        "op": "replace",
                        "path": "scenarios/task_a.json",
                        "expected_sha256": "stale",
                        "old": "A",
                        "new": "B",
                    }
                )
            with self.assertRaises(ValueError):
                workspace.action({"op": "write", "path": "../outside.py", "content": "bad"})
            self.assertEqual(len(workspace.validate(1)), 1)
            with self.assertRaises(ValueError):
                workspace.validate(0)
            workspace.snapshot(Path(tmp) / "snapshot")
            workspace.action(
                {
                    "op": "delete",
                    "path": "scenarios/task_a.json",
                    "expected_sha256": result["sha256"],
                }
            )
            self.assertTrue((Path(tmp) / "snapshot/scenarios/task_a.json").exists())

    def test_three_generations_two_reviews_and_two_evaluator_lanes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "run_agent.py").write_text("# architecture")
            (root / "src/evaluation").mkdir(parents=True)
            (root / "src/__init__.py").write_text("")
            for schema in ("scenario.schema.json",):
                (root / schema).write_bytes((ROOT / schema).read_bytes())
            calls = []

            class FakeAdapter:
                schema = json.loads((ROOT / "scenario.schema.json").read_text())

                def preflight(self, evaluator, output):
                    return {"passed": True}

                def execute_revision(self, evaluator, output, target, key):
                    calls.append(str(evaluator))
                    output.mkdir()
                    (output / "report.json").write_text(
                        json.dumps({"status": "complete", "observed": "task_a"})
                    )
                    return {"status": "complete"}

            class FakeClient:
                def __init__(self, *args):
                    self.generations = 0
                    self.reviews = 0

                def ask(self, messages):
                    task = json.loads(messages[1]["content"][0]["text"])
                    if task["task"].startswith("Generate"):
                        if len(messages) == 2:
                            return {"source": [{"path": "run_agent.py"}]}
                        if len(messages) == 4 and self.generations == 0:
                            return {
                                "actions": [
                                    {
                                        "op": "write",
                                        "path": "scenarios/task_a.json",
                                        "content": json.dumps(case()),
                                    }
                                ]
                            }
                        self.generations += 1
                        refs = json.loads(messages[3]["content"])
                        return {
                            "ready": True,
                            "rationale": [{"id": "task_a", "architecture_refs": [refs[0]["ref"]]}],
                            "coverage": {},
                            "changes": [],
                        }
                    if task["task"].startswith("Review"):
                        self.reviews += 1
                        ref = task["proposal"]["rationale"][0]["architecture_refs"][0]
                        assert (
                            Path(task["architecture_evidence"][ref]["path"])
                            == source / "run_agent.py"
                        )
                        assert "campaign_recovery" in task
                        role = task["task"].split("as ")[1].split(".")[0]
                        return {
                            "accepted": False,
                            "issues": [
                                {
                                    "id": role + "-one",
                                    "scenario_ids": ["task_a"],
                                    "description": "needs work",
                                }
                            ],
                        }
                    if len(messages) == 2:
                        return {"read": [{"id": next(iter(task["raw_evidence"]))}]}
                    ref = json.loads(messages[-1]["content"])[0]["ref"]
                    return {
                        "scenarios": [
                            {
                                "id": "task_a",
                                "property_outcome": "uncertain",
                                "evidence_refs": [ref],
                                "findings": [],
                            }
                        ],
                        "unresolved": [],
                        "converged": True,
                        "convergence_reason": "No further changes",
                        "proposed_changes": [],
                    }

            instances = []

            def factory(*args):
                instance = FakeClient()
                instances.append(instance)
                return instance

            config = {
                "scope": "test",
                "source": {},
                "orchestrator": {},
                "target": {},
                "adapter": {},
                "limits": {"rounds": 1},
            }
            with (
                patch("src.orchestrator.runner.ROOT", root),
                patch("src.orchestrator.runner.prepare_source", return_value=source),
            ):
                # Source inventory is normally produced by prepare_source.
                def prepare(config, output):
                    (output / "agent_source.json").write_text('{"files":["run_agent.py"]}')
                    return source

                with patch("src.orchestrator.runner.prepare_source", side_effect=prepare):
                    output = run(config, factory, lambda c: FakeAdapter(), execute=True)
            state = json.loads((output / "campaign.json").read_text())
            self.assertEqual(instances[0].generations, 3)
            self.assertEqual(instances[0].reviews, 6)
            self.assertEqual(len(calls), 2)
            self.assertEqual(state["rounds"][0]["approval"], "review_budget_exhausted")
            self.assertEqual(len(state["rounds"][0]["unresolved_review_findings"]), 3)
            self.assertEqual(state["stop_reason"], "orchestrator_converged")
            with (
                patch("src.orchestrator.runner.ROOT", root),
                patch("src.orchestrator.runner.prepare_source", side_effect=prepare),
                patch.object(
                    FakeAdapter, "preflight", side_effect=AssertionError("must not execute code")
                ),
                patch.object(
                    FakeAdapter,
                    "execute_revision",
                    side_effect=AssertionError("must not execute target"),
                ),
            ):
                generated = run(config, factory, lambda c: FakeAdapter(), execute=False)
            generated_state = json.loads((generated / "campaign.json").read_text())
            self.assertEqual(generated_state["stop_reason"], "generated_only")

    def test_transcript_preserves_calls_and_omits_duplicate_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace = root / "turn.jsonl"
            messages = [
                {"role": "system", "content": "PRIVATE SYSTEM PROMPT"},
                {"role": "user", "content": "Review the note"},
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning": "inspect this note",
                    "reasoning_content": "inspect this note",
                    "tool_calls": [{"id": "c1"}],
                },
                {"role": "tool", "tool_call_id": "c1", "content": "UNIQUE TOOL RESULT"},
                {"role": "assistant", "content": "Finished"},
            ]
            trace.write_text(
                json.dumps(
                    {
                        "event": "agent_result",
                        "result": {"messages": messages, "current_turn_user_idx": 1},
                    }
                )
                + "\n"
            )
            report = {
                "id": "a",
                "status": "passed",
                "scenario_contract": {},
                "turns": [
                    {
                        "turn_number": 1,
                        "sent_prompt": "Review the note",
                        "trace_path": str(trace),
                        "execution_events": [
                            {
                                "id": "c1",
                                "name": "read_file",
                                "arguments": {"path": "note.txt"},
                                "result": "UNIQUE TOOL RESULT",
                            }
                        ],
                        "final_answer": "Finished",
                    }
                ],
            }
            path = root / "report.json"
            path.write_text(json.dumps(report))
            text = render_phase(path)
            self.assertEqual(text.count("inspect this note"), 1)
            self.assertEqual(text.count("UNIQUE TOOL RESULT"), 1)
            self.assertEqual(text.count("Finished"), 1)
            self.assertIn("read_file", text)
            self.assertNotIn("PRIVATE SYSTEM PROMPT", text)
            self.assertNotIn("Literal callback", text)
            api = root / "api.jsonl"
            api.write_text(
                "\n".join(
                    json.dumps(e)
                    for e in [
                        {
                            "event": "api_request",
                            "request_id": "r1",
                            "turn_number": 1,
                            "phase": "agent",
                        },
                        {
                            "event": "api_response_body",
                            "request_id": "r1",
                            "turn_number": 1,
                            "phase": "agent",
                            "body": {
                                "choices": [
                                    {
                                        "message": {
                                            "role": "assistant",
                                            "reasoning": "reasoning retained only on the wire",
                                            "tool_calls": [
                                                {
                                                    "id": "c1",
                                                    "function": {
                                                        "name": "read_file",
                                                        "arguments": '{"path":"note.txt"}',
                                                    },
                                                }
                                            ],
                                        }
                                    }
                                ]
                            },
                        },
                    ]
                )
                + "\n"
            )
            report["api_trace_path"] = str(api)
            path.write_text(json.dumps(report))
            wire_text = render_phase(path)
            self.assertIn("reasoning retained only on the wire", wire_text)
            self.assertEqual(wire_text.count("UNIQUE TOOL RESULT"), 1)

    def test_generated_baseline_cannot_increase_fault_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp) / "working", ROOT)
            item = case()
            item["complexity"]["fault_count"] = 0
            item["experiment"] = {
                "branch_turn": 1,
                "intervention": "wrong direction",
                "baseline_complexity": {"fault_count": 1, "fault_types": ["outage"]},
                "baseline_turns": [
                    {
                        "turn_number": 1,
                        "pre_turn_perturbations": [
                            {"type": "set_fault_flag", "flag_name": "outage", "flag_value": True}
                        ],
                    }
                ],
            }
            workspace.action(
                {"op": "write", "path": "scenarios/task_a.json", "content": json.dumps(item)}
            )
            with self.assertRaisesRegex(ValueError, "baseline must not increase"):
                workspace.validate(1)

    def test_revisions_run_all_active_tests_and_keep_original_evaluator_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src/evaluation").mkdir(parents=True)
            (root / "src/__init__.py").write_text("")
            (root / "src/evaluation/evaluate.py").write_text("VERSION = 'original'\n")
            source = root / "source"
            source.mkdir()
            (source / "run_agent.py").write_text("# agent source")
            for name in ("scenario.schema.json",):
                (root / name).write_bytes((ROOT / name).read_bytes())
            executions = []

            class Adapter:
                schema = json.loads((ROOT / "scenario.schema.json").read_text())

                def preflight(self, *args):
                    return {"passed": True}

                def execute_revision(self, evaluator, output, *args):
                    version = runpy.run_path(str(evaluator / "src/evaluation/evaluate.py"))[
                        "VERSION"
                    ]
                    ids = sorted(p.stem for p in (evaluator / "scenarios").glob("*.json"))
                    executions.append((version, ids))
                    output.mkdir()
                    (output / "report.json").write_text(
                        json.dumps({"status": "complete", "version": version})
                    )
                    return {"status": "complete", "version": version}

            class Planner:
                def __init__(self, *args):
                    pass

                def ask(self, messages):
                    task = json.loads(messages[1]["content"][0]["text"])
                    if task["task"].startswith("Generate"):
                        number = task["round"]
                        if len(messages) == 2:
                            return {"source": [{"path": "run_agent.py"}]}
                        if len(messages) == 4:
                            item = case()
                            item["id"] = f"task_{number}"
                            prior = "original" if number == 1 else "candidate_1"
                            return {
                                "actions": [
                                    {
                                        "op": "write",
                                        "path": f"scenarios/task_{number}.json",
                                        "content": json.dumps(item),
                                    },
                                    {
                                        "op": "replace",
                                        "path": "src/evaluation/evaluate.py",
                                        "expected_sha256": task["files"][
                                            "src/evaluation/evaluate.py"
                                        ],
                                        "old": f"VERSION = '{prior}'",
                                        "new": f"VERSION = 'candidate_{number}'",
                                    },
                                ]
                            }
                        ref = json.loads(messages[3]["content"])[0]["ref"]
                        return {
                            "ready": True,
                            "rationale": [
                                {"id": f"task_{n}", "architecture_refs": [ref]}
                                for n in range(1, number + 1)
                            ],
                        }
                    if len(messages) == 2:
                        return {"read": [{"id": next(iter(task["raw_evidence"]))}]}
                    ref = json.loads(messages[-1]["content"])[0]["ref"]
                    return {
                        "scenarios": [
                            {
                                "id": s["id"],
                                "property_outcome": "uncertain",
                                "evidence_refs": [ref],
                                "findings": [],
                            }
                            for s in task["scenarios"]
                        ],
                        "unresolved": [],
                        "converged": False,
                        "convergence_reason": "Further coverage needed",
                        "proposed_changes": ["add coverage"],
                    }

            def prepare(config, output):
                (output / "agent_source.json").write_text('{"files":["run_agent.py"]}')
                return source

            config = {
                "scope": "test",
                "source": {},
                "orchestrator": {},
                "target": {},
                "adapter": {},
                "limits": {"rounds": 2, "generations": 1, "initial_tests": 1, "active_tests": 2},
            }
            with (
                patch("src.orchestrator.runner.ROOT", root),
                patch("src.orchestrator.runner.prepare_source", side_effect=prepare),
            ):
                output = run(config, Planner, lambda c: Adapter(), execute=True)
            state = json.loads((output / "campaign.json").read_text())
            self.assertEqual(state["stop_reason"], "execution_round_limit")
            self.assertEqual(
                executions,
                [
                    ("candidate_1", ["task_1"]),
                    ("original", ["task_1"]),
                    ("candidate_2", ["task_1", "task_2"]),
                    ("original", ["task_1", "task_2"]),
                ],
            )

    def test_evidence_ranges_and_changed_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "evidence.txt"
            p.write_text("abcdefgh", encoding="utf-8")
            evidence = Evidence(SecretRedactor())
            eid = evidence.add(p)
            chunk = evidence.read({"id": eid, "offset": 2, "length": 3})
            self.assertEqual(chunk["text"], "cde")
            self.assertTrue(chunk["more"])
            with self.assertRaises(ValueError):
                evidence.read({"id": eid, "offset": -1})
            p.write_text("modified", encoding="utf-8")
            with self.assertRaises(ValueError):
                evidence.read({"id": eid})

    def test_analysis_requires_inspected_citations_and_all_cases(self):
        evidence = Evidence(SecretRedactor())
        result = {
            "scenarios": [
                {
                    "id": "a",
                    "property_outcome": "uncertain",
                    "evidence_refs": ["invented"],
                    "findings": [],
                }
            ],
            "unresolved": [],
        }
        with self.assertRaises(ValueError):
            validate_analysis(result, evidence, ["a"])
        evidence.seen.add("invented")
        validate_analysis(result, evidence, ["a"])
        with self.assertRaises(ValueError):
            validate_analysis(result, evidence, ["a", "b"])
        result["scenarios"][0]["findings"] = [
            {
                "claim": "Service rejected a request",
                "contributors": ["runtime"],
                "certainty": "observed",
                "evidence_refs": ["invented"],
            }
        ]
        with self.assertRaises(ValueError):
            validate_analysis(result, evidence, ["a"], strict_owners=True)
        result["scenarios"][0]["findings"][0]["contributors"] = ["mock_service"]
        validate_analysis(result, evidence, ["a"], strict_owners=True)
        result["converged"] = True
        for invalid in (
            {"status": "invalid_evaluator"},
            {"status": "error"},
            {"status": "complete", "evidence_audit": {"passed": False}},
        ):
            with self.assertRaisesRegex(ValueError, "Cannot establish convergence"):
                validate_analysis(result, evidence, ["a"], executions={"candidate": invalid})
        validate_analysis(result, evidence, ["a"], executions={"candidate": {"status": "complete"}})
        result["converged"] = False
        validate_analysis(
            result, evidence, ["a"], executions={"candidate": {"status": "invalid_evaluator"}}
        )
        executions = {
            "candidate": {"status": "complete"},
            "trusted": {"status": "error"},
        }
        for gaps in (
            [],
            ["Execution failed"],
            [{"kind": "execution_validity", "lane": "candidate", "description": "Error"}],
            [{"kind": "execution_validity", "lane": "trusted", "description": " "}],
        ):
            result["unresolved"] = gaps
            with self.assertRaisesRegex(ValueError, "Retain incomplete/invalid lane"):
                validate_analysis(
                    result, evidence, ["a"], strict_owners=True, executions=executions
                )
        result["unresolved"] = [
            {"kind": "execution_validity", "lane": "trusted", "description": "Cleanup failed"}
        ]
        validate_analysis(result, evidence, ["a"], strict_owners=True, executions=executions)
        executions["candidate"]["evidence_audit"] = {"passed": False}
        with self.assertRaisesRegex(ValueError, "candidate"):
            validate_analysis(result, evidence, ["a"], strict_owners=True, executions=executions)

    def test_client_records_redacted_exchange_and_honors_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            response = httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]
                },
                request=httpx.Request("POST", "http://local/v1/chat/completions"),
            )
            client = Client(
                {"model": "planner", "base_url": "http://local/v1"},
                Path(tmp),
                SecretRedactor(["private-key"]),
                "private-key",
                1,
            )
            with patch("httpx.Client.post", return_value=response) as post:
                self.assertEqual(
                    client.ask([{"role": "user", "content": "private-key"}]), {"ok": True}
                )
                self.assertNotIn("max_tokens", post.call_args.kwargs["json"])
            self.assertNotIn("private-key", (Path(tmp) / "api_0001.request.json").read_text())
            with self.assertRaises(RuntimeError):
                client.ask([])

            # A caller's explicit limit is still honored; there is no hidden default.
            client.config["max_output_tokens"] = 321
            client.limit = 2
            with patch("httpx.Client.post", return_value=response) as post:
                client.ask([])
                self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 321)

    def test_client_accepts_provider_preamble_without_default_token_or_request_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            response = httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": 'Here is the result. {"ready":true}'},
                        }
                    ]
                },
                request=httpx.Request("POST", "http://local/v1/chat/completions"),
            )
            client = Client(
                {"model": "planner", "base_url": "http://local/v1"},
                Path(tmp),
                SecretRedactor(),
                "unused",
                None,
            )
            with patch("httpx.Client.post", return_value=response) as post:
                self.assertEqual(client.ask([]), {"ready": True})
                self.assertNotIn("max_tokens", post.call_args.kwargs["json"])
                self.assertNotIn("max_completion_tokens", post.call_args.kwargs["json"])


if __name__ == "__main__":
    unittest.main()
