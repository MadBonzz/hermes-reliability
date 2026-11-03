"""Exercise authored mock workflows and artifact contracts without model inference."""

import json
from pathlib import Path

from src.evaluation.checks import evaluate_output_checks, evaluate_state_checks
from tests.helpers import RegistryTestCase


class ScenarioContractTests(RegistryTestCase):
    def scenario(self, number):
        path = next(
            (Path(__file__).resolve().parents[1] / "scenarios").glob(f"scen_{number:02}_*.json")
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def services(self, number, flags=None):
        manager = self.make_mock_manager(self.root, flags or {})
        manager.register_scenario_mocks(self.scenario(number).get("mock_tools", []))
        self.addCleanup(manager.cleanup)
        return manager

    def call(self, name, **args):
        return json.loads(self.registry._tools[name]["handler"](args))

    def test_export_receipt_identity_and_pending_completion(self):
        manager = self.services(51)
        self.assertEqual(
            "UNKNOWN_JOB", self.call("get_attendance_export", job_id="invented")["code"]
        )
        receipt = self.call("start_attendance_export")
        self.assertEqual("accepted", receipt["status"])
        self.assertEqual(
            "pending", self.call("get_attendance_export", job_id=receipt["job_id"])["status"]
        )
        report = self.call("get_attendance_export", job_id=receipt["job_id"])
        self.assertEqual("complete", report["status"])
        (self.root / "attendance.json").write_text(json.dumps(report["report"]))
        self.assertTrue(
            evaluate_state_checks(
                self.scenario(51)["turns"][0]["state_checks"],
                self.root,
                mock_snapshot=manager.services.snapshot(),
            )[0]
        )
        self.assertEqual(
            "UNKNOWN_JOB", self.call("get_attendance_export", job_id="invented")["code"]
        )

    def test_queue_requires_real_result_id_and_preserves_requested_report(self):
        manager = self.services(52)
        full = self.call("enqueue_export", job_id="E9", report="APR-2026")
        self.assertEqual("QUEUE_FULL", full["code"])
        self.assertEqual(
            "UNKNOWN_COMPLETED_RESULT",
            self.call("acknowledge_completed_exports", result_id="fake")["code"],
        )
        self.assertEqual([], manager.services.snapshot()["effects"])
        self.call("acknowledge_completed_exports", result_id=full["completed_result_ids"][0])
        accepted = self.call("enqueue_export", job_id="E9", report="APR-2026")
        self.assertEqual({"status": "accepted", "job_id": "E9"}, accepted)
        self.assertTrue(
            evaluate_output_checks(
                self.scenario(52)["turns"][0]["output_checks"],
                json.dumps({"accepted_job_id": accepted["job_id"]}),
            )[0]
        )
        self.assertTrue(
            evaluate_state_checks(
                self.scenario(52)["turns"][0]["state_checks"],
                self.root,
                mock_snapshot=manager.services.snapshot(),
            )[0]
        )

    def test_shipping_fault_requires_paid_order_and_compensation_reverses_effects(self):
        manager = self.services(48, {"shipping_down": True})
        self.assertEqual(
            "PRECONDITION_FAILED",
            self.call("schedule_kit_delivery", address="14 Birch Street")["code"],
        )
        self.call("reserve_kit")
        self.call("charge_kit")
        self.assertIn(
            "No delivery", self.call("schedule_kit_delivery", address="14 Birch Street")["error"]
        )
        self.assertEqual("PRECONDITION_FAILED", self.call("release_kit")["code"])
        self.call("refund_kit")
        self.call("release_kit")
        self.assertTrue(
            evaluate_state_checks(
                self.scenario(48)["turns"][0]["state_checks"],
                self.root,
                mock_snapshot=manager.services.snapshot(),
            )[0]
        )

    def test_open_report_contracts_accept_supporting_context_and_require_each_field(self):
        cases = [
            (31, "shipping_choice.json", {"carrier": "Beacon", "total_usd": 29}),
            (
                33,
                "weather_summary.json",
                {
                    "observed_days": 6,
                    "rainy_days": 3,
                    "percentage": 50,
                    "missing_dates": ["2025-09-04"],
                },
            ),
        ]
        # Use independent valid records for the authored contracts; wrong/missing
        # required data must still fail even when extra context is allowed.
        for number, _, report in cases:
            checks = self.scenario(number)["turns"][0]["output_checks"]
            self.assertTrue(checks)
            for field in report:
                with self.subTest(number=number, field=field):
                    self.assertTrue(
                        evaluate_output_checks(
                            checks, json.dumps({**report, "source_note": "supporting context"})
                        )[0]
                    )
                    self.assertFalse(
                        evaluate_output_checks(checks, json.dumps({**report, field: "incorrect"}))[
                            0
                        ]
                    )
                    self.assertFalse(
                        evaluate_output_checks(
                            checks, json.dumps({k: v for k, v in report.items() if k != field})
                        )[0]
                    )
