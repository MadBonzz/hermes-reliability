import hashlib
import json

from src.attribute_failures import attribute_turn, contract_for
from tests.helpers import TemporaryDirectoryTestCase


class AttributionTests(TemporaryDirectoryTestCase):
    def record(self):
        return {
            "turn_number": 1,
            "origin": "model",
            "gate_passed": False,
            "execution_events": [],
            "mock_snapshot": {"effects": []},
        }

    def wire(self, name, args, call_id="c", schema=None):
        return [
            {
                "event": "api_request",
                "request_id": call_id,
                "turn_number": 1,
                "phase": "agent",
                "body": {
                    "tools": [
                        {"function": {"name": name, "parameters": schema or {"type": "object"}}}
                    ]
                },
            },
            {
                "event": "api_response_body",
                "request_id": call_id,
                "body": {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": call_id,
                                        "function": {"name": name, "arguments": json.dumps(args)},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            },
        ]

    def test_invalid_served_arguments_and_rejection_are_separate_findings(self):
        record = self.record()
        record["gate_passed"] = True  # Recovery must not erase the intermediate error.
        record["execution_events"] = [
            {
                "id": "c",
                "name": "write_file",
                "arguments": {"content": {}},
                "result": '{"error":"content must be string"}',
                "completed": True,
            }
        ]
        wire = self.wire(
            "write_file",
            {"content": {}},
            schema={"type": "object", "properties": {"content": {"type": "string"}}},
        )
        result = attribute_turn(record, {}, {}, wire, [], "traces/s")
        self.assertEqual(
            ["served_model_output", "runtime_tool"], [f["layer"] for f in result["findings"]]
        )
        self.assertEqual("error_returned_by_tool", result["findings"][0]["disposition"])
        self.assertTrue(result["task_gate_passed"])

    def test_required_call_contradiction_is_verifier_evidence(self):
        record = self.record()
        record["execution_events"] = [
            {"id": "c", "name": "get_event", "result": "{}", "completed": True}
        ]
        record["process_check"] = {"details": ["[FAIL] tool_called('get_event') in ['tool_call']"]}
        spec = {"process_checks": [{"type": "tool_called", "tool_name": "get_event"}]}
        findings = attribute_turn(record, spec, {}, [], [], "traces/s")["findings"]
        self.assertEqual(["required_call_false_negative"], [f["kind"] for f in findings])
        record["execution_events"] = []
        self.assertEqual([], attribute_turn(record, spec, {}, [], [], "traces/s")["findings"])

    def test_duplicate_has_joint_attribution_but_legitimate_repetition_does_not(self):
        contract = {
            "mock_tools": [
                {
                    "name": "pay",
                    "behavior": {"operation": "idempotent_append", "idempotency_field": "key"},
                }
            ]
        }
        record = self.record()
        record["mock_snapshot"]["effects"] = [{"tool": "pay"}, {"tool": "pay"}]
        wire = self.wire("pay", {"key": "first", "amount": 25}, "a") + self.wire(
            "pay", {"key": "second", "amount": 25}, "b"
        )
        trace = [{"event": "before_agent", "mock_snapshot": {"effects": []}}]
        spec = {"state_checks": [{"type": "mock_effects_equal", "expected": ["pay"]}]}
        findings = attribute_turn(record, spec, contract, wire, trace, "traces/s")["findings"]
        self.assertEqual(
            ["retry_identity_changed", "application_duplicate_effects"],
            [f["kind"] for f in findings],
        )
        self.assertEqual("application_workflow", findings[1]["layer"])
        self.assertEqual(
            "not_applicable_without_explicit_runtime_contract", findings[1]["native_hermes_bug"]
        )
        spec["state_checks"][0]["expected"] = ["pay", "pay"]
        self.assertEqual(
            [], attribute_turn(record, spec, contract, wire, trace, "traces/s")["findings"]
        )

    def test_guard_refusal_does_not_prove_correct_enforcement_or_bug(self):
        record = self.record()
        record["execution_events"] = [
            {"id": "x", "name": "write_file", "result": {"stale_write_blocked": True}}
        ]
        finding = attribute_turn(record, {}, {}, [], [], "traces/s")["findings"][0]
        self.assertEqual("overwrite_guard_rejection", finding["kind"])
        self.assertIn("unresolved", finding["limitation"])

    def test_provider_rejection_is_not_blame_on_model(self):
        events = self.wire("read_file", {})[:1]
        events.append({"event": "api_response_headers", "request_id": "c", "status_code": 429})
        findings = attribute_turn(self.record(), {}, {}, events, [], "traces/s")["findings"]
        self.assertEqual(["provider_or_configuration"], [f["layer"] for f in findings])

    def test_old_contract_requires_exact_hash_and_embedded_contract_is_preferred(self):
        path = self.root / "s.json"
        path.write_text('{"id":"s","turns":[]}')
        report = {
            "scenario_files": [{"id": "s", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]
        }
        self.assertEqual("source_hash_verified", contract_for(report, {"id": "s"}, self.root)[1])
        path.write_text('{"id":"s","turns":[{}]}')
        self.assertIsNone(contract_for(report, {"id": "s"}, self.root)[0])
        self.assertEqual(
            "frozen_in_report",
            contract_for(report, {"id": "s", "scenario_contract": {}}, self.root)[1],
        )
