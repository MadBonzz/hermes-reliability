import copy
import csv
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from src.analyze_v2 import summarize
from src.benchmark_v2 import condition_spec, validate_prefix, validate_v2, visible_input_validator
from src.checks import evaluate_output_checks, evaluate_state_checks
from src.eval_runner import parse_args
from src.eval_v2 import phase_workspace, run_case
from src.evaluation_metrics import efficiency, injection_evidence
from src.scenario_validation import load_scenarios
from tests.helpers import RegistryTestCase, TemporaryDirectoryTestCase

ROOT = Path(__file__).resolve().parents[1]


def spec(number):
    return json.loads(
        next((ROOT / "scenarios_v2").glob(f"v2_scen_{number:02}_*.json")).read_text(
            encoding="utf-8"
        )
    )


class BranchTests(TemporaryDirectoryTestCase):
    def test_phase_cleanup_retries_windows_handle_locks(self):
        locked = PermissionError("directory handle still open")
        locked.winerror = 32
        workspace = Mock(name="temporary_directory")
        workspace.name = "temporary-phase"
        workspace.cleanup.side_effect = [locked, None]
        with (
            patch("src.eval_v2.tempfile.TemporaryDirectory", return_value=workspace),
            patch("src.eval_v2.time.sleep") as sleep,
        ):
            with phase_workspace() as root:
                self.assertEqual(root, "temporary-phase")
            self.assertEqual(workspace.cleanup.call_count, 2)
            sleep.assert_called_once_with(1)

    def test_phase_cleanup_retains_permanent_failures(self):
        for code, expected_calls in ((32, 5), (5, 1)):
            denied = PermissionError("cleanup failed")
            denied.winerror = code
            workspace = Mock()
            workspace.cleanup.side_effect = denied
            with (
                patch("src.eval_v2.tempfile.TemporaryDirectory", return_value=workspace),
                patch("src.eval_v2.time.sleep") as sleep,
                self.assertRaises(PermissionError),
                phase_workspace(),
            ):
                pass
            self.assertEqual(workspace.cleanup.call_count, expected_calls)
            self.assertEqual(sleep.call_count, expected_calls - 1)

    def test_all_v2_answer_contracts_reject_misleading_values(self):
        suite, _ = load_scenarios(ROOT / "scenarios_v2", ROOT / "scenario_v2.schema.json")
        for source in suite["scenarios"]:
            conditions = ("baseline", "treatment") if source.get("experiment") else ("treatment",)
            for condition in conditions:
                for turn in condition_spec(source, condition)["turns"]:
                    with self.subTest(
                        scenario=source["id"], condition=condition, turn=turn["turn_number"]
                    ):
                        checks = turn["output_checks"]
                        self.assertTrue(
                            all(c["type"] in ("json_answer", "not_contains") for c in checks)
                        )
                        if "reference" in turn:
                            self.assertTrue(
                                evaluate_output_checks(checks, turn["reference"]["final_response"])[
                                    0
                                ]
                            )
                        answers = [c for c in checks if c["type"] == "json_answer"]
                        if not answers:
                            continue
                        if "json_path" not in answers[0]:
                            correct = answers[0]["expected"]
                        else:
                            correct = {c["json_path"]: c["expected"] for c in answers}
                        self.assertTrue(evaluate_output_checks(checks, json.dumps(correct))[0])
                        self.assertFalse(evaluate_output_checks(checks, "")[0])
                        self.assertFalse(
                            evaluate_output_checks(checks, '[{"tool_name":"read_file"}]')[0]
                        )
                        for check in answers:
                            expected = check["expected"]
                            if isinstance(expected, bool):
                                wrong_values = [not expected, int(expected)]
                            elif isinstance(expected, (int, float)):
                                wrong_values = [expected + 0.99, str(expected), True]
                            elif isinstance(expected, str):
                                wrong_values = [expected + "0", "not " + expected]
                            elif isinstance(expected, list):
                                wrong_values = [expected + (expected[:1] or ["unexpected"])]
                                if expected:
                                    wrong_values.append(expected[:-1])
                                if check.get("unordered"):
                                    reordered = list(reversed(expected))
                                    answer = (
                                        {**correct, check["json_path"]: reordered}
                                        if "json_path" in check
                                        else reordered
                                    )
                                    self.assertTrue(
                                        evaluate_output_checks(checks, json.dumps(answer))[0]
                                    )
                            else:
                                wrong_values = ["unknown"]
                            for wrong in wrong_values:
                                answer = (
                                    {
                                        **correct,
                                        check["json_path"]: wrong,
                                        "unrelated_value": expected,
                                    }
                                    if "json_path" in check
                                    else wrong
                                )
                                self.assertFalse(
                                    evaluate_output_checks(checks, json.dumps(answer))[0], answer
                                )

    def test_all_v2_file_arrays_preserve_multiset_contracts(self):
        suite, _ = load_scenarios(ROOT / "scenarios_v2", ROOT / "scenario_v2.schema.json")
        for source in suite["scenarios"]:
            conditions = ("baseline", "treatment") if source.get("experiment") else ("treatment",)
            for condition in conditions:
                for turn in condition_spec(source, condition)["turns"]:
                    for original in turn["state_checks"]:
                        if original["type"] != "json_array_unordered":
                            continue
                        check = {**original, "path": "array.json"}
                        check.pop("json_path", None)
                        expected = check["expected"]
                        path = self.root / "array.json"
                        with self.subTest(
                            scenario=source["id"], turn=turn["turn_number"], path=original["path"]
                        ):
                            path.write_text(json.dumps(list(reversed(expected))), encoding="utf-8")
                            self.assertTrue(evaluate_state_checks([check], self.root)[0])
                            for wrong in (
                                expected + (expected[:1] or ["unexpected"]),
                                {"items": expected},
                            ):
                                path.write_text(json.dumps(wrong), encoding="utf-8")
                                self.assertFalse(evaluate_state_checks([check], self.root)[0])
                            if (
                                expected
                                and isinstance(expected[0], (str, int))
                                and "item_key" not in check
                            ):
                                path.write_text(
                                    json.dumps([{"id": v} for v in expected]), encoding="utf-8"
                                )
                                self.assertFalse(evaluate_state_checks([check], self.root)[0])

    def test_release_and_confirmation_answers_require_explicit_outcomes(self):
        for number in (62, 93, 95, 97, 99, 101, 103):
            turn = spec(number)["turns"][-1]
            checks = turn["output_checks"]
            expected = {check["json_path"]: check["expected"] for check in checks}
            for field in expected:
                self.assertIn(field, turn["prompt"])
            self.assertTrue(evaluate_output_checks(checks, json.dumps(expected))[0])
            for answer in (
                "No additional (potentially invalid) proposal exists to process.",
                '[{"tool_name": "read_file", "parameters": {"path": "R4.json"}}]',
                "",
            ):
                self.assertFalse(evaluate_output_checks(checks, answer)[0])
            for field, value in expected.items():
                wrong = {**expected, field: not value if isinstance(value, bool) else value + 1}
                self.assertFalse(evaluate_output_checks(checks, json.dumps(wrong))[0])

    def test_rejected_package_entries_accept_paths_and_entry_objects(self):
        check = spec(68)["turns"][0]["state_checks"][1]
        path = self.root / check["path"]
        rejected = "../service_b/config.json"
        for value, valid in (
            ([rejected], True),
            ([{"target_path": rejected, "content": "REPLACE OTHER PROJECT"}], True),
            ([{"content": "missing path"}], False),
            ([{"target_path": "reports/month.txt"}], False),
            ([rejected, {"target_path": rejected}], False),
            ({"target_path": rejected}, False),
        ):
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(valid, evaluate_state_checks([check], self.root)[0], value)

    def test_csv_and_log_complexity_counts_data_records(self):
        for number in (45, 58, 64, 79):
            scenario = spec(number)
            count = 0
            for fixture in scenario["turns"][0]["pre_turn_perturbations"]:
                path = fixture.get("path", "")
                if path.endswith(".csv"):
                    count += len(list(csv.DictReader(io.StringIO(fixture["content"]))))
                elif path.endswith(".log"):
                    count += len([line for line in fixture["content"].splitlines() if line.strip()])
            self.assertEqual(count, scenario["complexity"]["records"], scenario["id"])

    def test_scaled_numeric_answers_reject_wrong_amounts_and_unrelated_numbers(self):
        for number in (93, 95, 97, 99, 101, 103):
            turn = spec(number)["turns"][0]
            checks = turn["output_checks"]
            field, expected = checks[0]["json_path"], checks[0]["expected"]
            self.assertIn(field, turn["prompt"])
            self.assertTrue(evaluate_output_checks(checks, turn["reference"]["final_response"])[0])
            wrong = json.dumps({field: expected + 0.99, "unrelated_number": expected})
            self.assertFalse(evaluate_output_checks(checks, wrong)[0])
            self.assertFalse(evaluate_output_checks(checks, f"The wrong total is {expected}.99")[0])

    def test_retirement_updates_allow_reordering_but_preserve_records(self):
        for number in range(98, 104):
            scenario = spec(number)
            turn = scenario["turns"][0 if len(scenario["turns"]) == 1 else 1]
            self.assertIn("Remove customer U1 from", turn["prompt"])
            for check in turn["state_checks"]:
                self.assertEqual("json_array_unordered", check["type"])
                path = self.root / check["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                rows = list(reversed(check["expected"]))
                path.write_text(json.dumps(rows), encoding="utf-8")
                self.assertTrue(evaluate_state_checks([check], self.root)[0])
                path.write_text(json.dumps(rows + [rows[0]]), encoding="utf-8")
                self.assertFalse(evaluate_state_checks([check], self.root)[0])

    def test_v2_cli_uses_shared_options_and_rejects_legacy_workspace_options(self):
        _, args = parse_args(["--repetitions", "2", "--order-seed", "7"], v2=True)
        self.assertEqual(str(ROOT / "scenarios_v2"), args.scenarios)
        self.assertEqual((2, 7), (args.repetitions, args.order_seed))
        help_output = io.StringIO()
        with redirect_stdout(help_output), self.assertRaises(SystemExit) as exit:
            parse_args(["--help"], v2=True)
        self.assertEqual(0, exit.exception.code)
        for option in ("--model", "--provider", "--repetitions", "--prices"):
            self.assertIn(option, help_output.getvalue())
        for options in (
            ["--workspace-root", "unused"],
            ["--keep-workspace"],
            ["--repetitions", "0"],
        ):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
                parse_args(options, v2=True)
            self.assertEqual(2, exit.exception.code)

    def test_authored_conditions_validate_and_baseline_checks_differ(self):
        suite, _ = load_scenarios(ROOT / "scenarios_v2", ROOT / "scenario_v2.schema.json")
        validate_v2(suite, ROOT / "scenario_v2.schema.json")
        event = spec(41)
        baseline = condition_spec(event, "baseline")
        self.assertEqual(0, baseline["complexity"]["fault_count"])
        self.assertEqual(1, event["complexity"]["fault_count"])
        self.assertEqual(event["turns"][0], baseline["turns"][0])
        self.assertEqual([], baseline["turns"][1]["pre_turn_perturbations"])
        self.assertEqual("Cedar", baseline["turns"][1]["state_checks"][0]["expected"]["room"])

    def test_prefix_sampled_once_and_branches_have_independent_history_and_service_state(self):
        scenario = spec(41)
        invocations = []
        initial = {
            "history": [
                {"role": "user", "content": "Review"},
                {"role": "assistant", "content": "Cedar, 100"},
            ],
            "services": {"states": {"event": {"version": 1}}},
            "fault_flags": {},
        }

        def launch(config, key):
            invocations.append(copy.deepcopy(config))
            if config.get("stop_before_turn"):
                return {
                    "status": "prefix_ready",
                    "turns": [
                        {
                            "gate_passed": True,
                            "execution_events": [{"name": "get_event", "completed": True}],
                            "workspace_after_agent": {".": {"kind": "directory"}},
                        }
                    ],
                    "continuation": copy.deepcopy(initial),
                }
            self.assertEqual(initial, config["continuation"])
            config["continuation"]["history"][0]["content"] = "branch modification"
            config["continuation"]["services"]["states"]["event"]["version"] = 99
            self.assertEqual([2], [t["turn_number"] for t in config["scenario"]["turns"]])
            return {"status": "passed", "turns": []}

        entry = run_case(scenario, {"output": str(self.root)}, "key", launch=launch)
        self.assertEqual(3, len(invocations))
        self.assertEqual("sampled_prefix", entry["branch_origin"])
        self.assertEqual(initial, entry["shared_prefix"]["continuation"])

    def test_unsafe_prefix_is_rejected_instead_of_claiming_runtime_clone(self):
        report = {
            "status": "prefix_ready",
            "turns": [
                {
                    "gate_passed": True,
                    "execution_events": [{"name": "execute_code"}],
                    "workspace_after_agent": {},
                }
            ],
        }
        with self.assertRaises(ValueError):
            validate_prefix(report, spec(41))

    def test_failed_prefix_is_retained_and_reference_origin_explicit(self):
        count = 0

        def launch(config, key):
            nonlocal count
            count += 1
            if count == 1:
                return {"status": "failed", "turns": []}
            if count == 2:
                self.assertEqual(1, config["reference_until"])
                return {
                    "status": "prefix_ready",
                    "reference_prefix": [
                        {
                            "gate_passed": True,
                            "execution_events": [{"name": "get_event"}],
                            "workspace_after_agent": {},
                        }
                    ],
                    "continuation": {
                        "history": [{"role": "assistant", "content": "Cedar, 100"}],
                        "services": {},
                        "fault_flags": {},
                    },
                }
            return {"status": "passed"}

        entry = run_case(spec(41), {"output": str(self.root)}, "key", launch=launch)
        self.assertEqual(4, count)
        self.assertEqual("failed", entry["shared_prefix"]["status"])
        self.assertEqual("authored_reference", entry["branch_origin"])

    def test_visible_identity_is_rejected_before_model_dispatch(self):
        check = visible_input_validator(spec(41), ROOT)
        check({"messages": [{"role": "user", "content": "Reduce capacity to 80."}]})
        for text in (
            "[EVAL run=abc]",
            "scen_41_versioned_event_edit",
            str(ROOT),
            "eval_harness/results",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                check({"messages": [{"role": "tool", "content": text}]})

    def test_disabled_memory_refusal_needs_no_persistent_state_clone(self):
        call = {
            "name": "memory",
            "completed": True,
            "result": json.dumps(
                {
                    "error": "Memory is not available. It may be disabled in config or this environment.",
                    "success": False,
                }
            )
            + "\n[Tool loop warning]",
        }
        report = {
            "status": "prefix_ready",
            "turns": [
                {"gate_passed": True, "execution_events": [call], "workspace_after_agent": {}}
            ],
            "continuation": {"history": [{"role": "assistant", "content": "Cedar, 100"}]},
        }
        validate_prefix(report, spec(41))
        call["result"] = json.dumps({"success": True})
        with self.assertRaises(ValueError):
            validate_prefix(report, spec(41))

    def test_json_answer_accepts_formatting_extra_fields_and_typed_values(self):
        check = [{"type": "json_answer", "json_path": "count", "expected": 1}]
        self.assertTrue(evaluate_output_checks(check, '```json\n{"count":1,"note":"ok"}\n```')[0])
        for answer in ('{"count":true}', "{}", "not JSON"):
            self.assertFalse(evaluate_output_checks(check, answer)[0])


class CheckpointTests(RegistryTestCase):
    def test_service_checkpoint_preserves_replay_receipt_counters_and_independence(self):
        manager = self.make_mock_manager(self.root, {})
        source = spec(47)
        manager.register_scenario_mocks(source["mock_tools"])
        self.addCleanup(manager.cleanup)
        handler = self.registry._tools["pay_invoice"]["handler"]
        args = {"request_id": "one", "invoice_id": "INV-A", "amount_usd": 25}
        response = json.loads(handler(args))
        checkpoint = json.loads(json.dumps(manager.services.checkpoint()))
        json.loads(handler({**args, "request_id": "two"}))
        manager.services.restore(checkpoint)
        replay = json.loads(handler(args))
        self.assertEqual(response["receipt_id"], replay["receipt_id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(1, len(manager.services.effects))
        next_receipt = json.loads(handler({**args, "request_id": "new", "invoice_id": "INV-B"}))
        self.assertEqual("PAY-000002", next_receipt["receipt_id"])
        self.assertEqual(1, len(checkpoint["effects"]))

    def test_scaled_settlement_checks_real_identity_and_matching_receipts(self):
        source = spec(92)
        manager = self.make_mock_manager(self.root, {})
        manager.register_scenario_mocks(source["mock_tools"])
        self.addCleanup(manager.cleanup)
        check = source["turns"][0]["state_checks"][:1]
        invoice = {
            "invoice_id": "R1",
            "recipient": "Person 1",
            "amount_usd": 7,
            "status": "approved",
        }
        (self.root / "invoices").mkdir()
        args = {k: invoice[k] for k in ("invoice_id", "recipient", "amount_usd")}
        args["request_id"] = "one"
        result = json.loads(self.registry._tools["settle_reimbursement"]["handler"](args))
        artifact = {**invoice, "status": "paid", "receipt_id": result["receipt_id"]}
        target = self.root / "invoices/R1.json"
        target.write_text(json.dumps(artifact))
        self.assertTrue(
            evaluate_state_checks(check, self.root, mock_snapshot=manager.services.snapshot())[0]
        )
        for field, wrong in (
            ("receipt_id", "fabricated"),
            ("recipient", "Wrong person"),
            ("amount_usd", 999),
        ):
            target.write_text(json.dumps({**artifact, field: wrong}))
            self.assertFalse(
                evaluate_state_checks(check, self.root, mock_snapshot=manager.services.snapshot())[
                    0
                ]
            )


class EfficiencyTests(TemporaryDirectoryTestCase):
    def report(self, missing=False):
        path = self.root / "api.jsonl"
        events = [
            {"event": "api_request", "request_id": "a", "body": {"model": "m"}},
            {
                "event": "api_response_body",
                "request_id": "a",
                "body": {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "prompt_tokens_details": {"cached_tokens": 30},
                    },
                },
            },
        ]
        if missing:
            events.append({"event": "api_request", "request_id": "b", "body": {"model": "m"}})
        path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
        call = {"name": "read_file", "arguments": {"path": "a.txt"}, "result": "hello"}
        return {
            "api_trace_path": str(path),
            "turns": [
                {
                    "turn_number": 1,
                    "execution_events": [call, call, call],
                    "tool_calls": [call],
                    "elapsed_sec": 4,
                }
            ],
        }

    def test_all_calls_and_cached_tokens_priced_once(self):
        rates = {
            "m": {
                "input_per_million_usd": 2,
                "output_per_million_usd": 10,
                "cached_input_per_million_usd": 0.5,
            }
        }
        metrics = efficiency(self.report(), rates)
        self.assertEqual(3, metrics["executed_tool_calls"])
        self.assertEqual(2, metrics["repeated_identical_calls"])
        self.assertAlmostEqual((70 * 2 + 30 * 0.5 + 20 * 10) / 1e6, metrics["estimated_cost_usd"])
        self.assertEqual(1, metrics["observed_file_tool_path_count"])
        incomplete = efficiency(self.report(missing=True), rates)
        self.assertIsNone(incomplete["estimated_cost_usd"])
        self.assertFalse(incomplete["usage_complete"])

    def test_missing_cache_price_or_model_price_is_unpriced(self):
        self.assertIsNone(
            efficiency(
                self.report(), {"m": {"input_per_million_usd": 1, "output_per_million_usd": 2}}
            )["estimated_cost_usd"]
        )
        self.assertIsNone(efficiency(self.report())["estimated_cost_usd"])

    def test_shared_prefix_cost_is_counted_once(self):
        phase = {
            "status": "passed",
            "efficiency": {"api_requests": 2, "estimated_cost_usd": 0.1, "usage_complete": True},
        }
        report = {
            "cases": [
                {
                    "id": "one",
                    "complexity": spec(41)["complexity"],
                    "shared_prefix": phase,
                    "branches": {"baseline": phase, "treatment": phase},
                    "branch_origin": "sampled_prefix",
                }
            ]
        }
        totals = summarize(report)["total_actual_execution_cost"]
        self.assertEqual(6, totals["api_requests"])
        self.assertAlmostEqual(0.3, totals["estimated_cost_usd"])

    def test_safe_injection_outcome_does_not_establish_awareness(self):
        report = self.report()
        report["turns"][0]["execution_events"][0]["result"] = "INJECTED_TEXT"
        evidence = injection_evidence(
            report, {"payload_marker": "INJECTED_TEXT", "harmful_tools": ["send_email"]}
        )
        self.assertEqual("observed_tool_result", evidence["exposure"])
        self.assertEqual("unestablished", evidence["recognition"])
