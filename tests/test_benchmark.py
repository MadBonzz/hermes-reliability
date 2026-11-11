import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from src.evaluation.checks import evaluate_output_checks
from src.evaluation.evaluate import parse_args, phase_workspace
from src.evaluation.evaluation_metrics import api_metrics, efficiency, injection_evidence
from tests.helpers import RegistryTestCase, TemporaryDirectoryTestCase

ROOT = Path(__file__).resolve().parents[1]


class BranchTests(TemporaryDirectoryTestCase):
    def test_api_usage_is_independent_of_content_shape_and_rejects_invalid_counts(self):
        request = {"event": "api_request", "request_id": "a", "body": {"model": "model"}}
        body = {
            "event": "api_response_body",
            "request_id": "a",
            "body": {
                "choices": [{"message": {"content": [{"type": "text", "text": "answer"}]}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5},
            },
        }
        self.assertEqual(api_metrics([request, body])["reported_tokens"]["output_tokens"], 5)
        for usage in (
            [1],
            {"prompt_tokens": -1, "completion_tokens": 5},
            {"prompt_tokens": 3, "completion_tokens": 5, "prompt_tokens_details": [1]},
        ):
            body["body"]["usage"] = usage
            self.assertFalse(api_metrics([request, body])["usage_complete"])

    def test_phase_cleanup_retries_windows_handle_locks(self):
        locked = PermissionError("directory handle still open")
        locked.winerror = 32
        workspace = Mock(name="temporary_directory")
        workspace.name = "temporary-phase"
        workspace.cleanup.side_effect = [locked, None]
        with (
            patch("src.evaluation.evaluate.tempfile.TemporaryDirectory", return_value=workspace),
            patch("src.evaluation.evaluate.time.sleep") as sleep,
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
                patch(
                    "src.evaluation.evaluate.tempfile.TemporaryDirectory", return_value=workspace
                ),
                patch("src.evaluation.evaluate.time.sleep") as sleep,
                self.assertRaises(PermissionError),
                phase_workspace(),
            ):
                pass
            self.assertEqual(workspace.cleanup.call_count, expected_calls)
            self.assertEqual(sleep.call_count, expected_calls - 1)

    def test_cli_uses_shared_options_and_rejects_legacy_workspace_options(self):
        _, args = parse_args(["--repetitions", "2", "--order-seed", "7"])
        self.assertEqual(str(ROOT / "scenarios"), args.scenarios)
        self.assertEqual((2, 7), (args.repetitions, args.order_seed))
        help_output = io.StringIO()
        with redirect_stdout(help_output), self.assertRaises(SystemExit) as exit:
            parse_args(["--help"])
        self.assertEqual(0, exit.exception.code)
        for option in ("--model", "--provider", "--repetitions", "--prices"):
            self.assertIn(option, help_output.getvalue())
        for options in (
            ["--workspace-root", "unused"],
            ["--keep-workspace"],
            ["--repetitions", "0"],
        ):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
                parse_args(options)
            self.assertEqual(2, exit.exception.code)

    def test_json_answer_accepts_formatting_extra_fields_and_typed_values(self):
        check = [{"type": "json_answer", "json_path": "count", "expected": 1}]
        self.assertTrue(evaluate_output_checks(check, '```json\n{"count":1,"note":"ok"}\n```')[0])
        for answer in ('{"count":true}', "{}", "not JSON"):
            self.assertFalse(evaluate_output_checks(check, answer)[0])


class CheckpointTests(RegistryTestCase):
    pass


class EfficiencyTests(TemporaryDirectoryTestCase):
    def test_native_failures_use_explicit_signals_not_error_words(self):
        cases = [
            (
                {"result": "An error occurred", "native_observation_status": {"is_error": True}},
                True,
            ),
            (
                {
                    "result": "test failed",
                    "native_observation_status": {"is_error": False, "exit_code": 1},
                },
                True,
            ),
            ({"native_result": {"status": "error"}}, True),
            ({"error": "Tool execution raised"}, True),
            (
                {
                    "result": '{"error":"busy"}',
                    "native_observation_status": {"is_error": True, "exit_code": 1},
                },
                True,
            ),
            ({"native_observation_status": {"timeout": True}}, True),
            (
                {
                    "result": "Read a log containing ERROR and timeout",
                    "native_observation_status": {"is_error": False, "exit_code": 0},
                },
                False,
            ),
            ({"native_observation_status": {"exit_code": None}}, False),
            ({"result": "No explicit status available"}, False),
            ({"native_observation_status": {"exit_code": True}}, False),
        ]
        report = self.report()
        report["turns"][0]["execution_events"] = [
            {"name": "native_tool", "arguments": {}, **call} for call, _ in cases
        ]
        metrics = efficiency(report)
        self.assertEqual(sum(failed for _, failed in cases), metrics["failed_tool_results"])
        self.assertEqual(
            {"tool_error": 4, "command_nonzero_exit": 2, "timeout": 1},
            metrics["tool_failure_signals"],
        )

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

    def test_safe_injection_outcome_does_not_establish_awareness(self):
        report = self.report()
        report["turns"][0]["execution_events"][0]["result"] = "INJECTED_TEXT"
        evidence = injection_evidence(
            report, {"payload_marker": "INJECTED_TEXT", "harmful_tools": ["send_email"]}
        )
        self.assertEqual("observed_tool_result", evidence["exposure"])
        self.assertEqual("unestablished", evidence["recognition"])
