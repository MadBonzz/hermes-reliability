import json

from src.audit_api_evidence import audit, audit_run, reconstruct_responses
from tests.helpers import TemporaryDirectoryTestCase


class EvidenceAuditTests(TemporaryDirectoryTestCase):
    def test_stream_reassembly_preserves_split_arguments_reasoning_and_usage(self):
        chunks = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "reasoning_content": "think ",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c",
                                    "function": {"name": "read_file", "arguments": '{"path":'},
                                }
                            ],
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "reasoning_content": "more",
                            "tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {"choices": [], "usage": {"total_tokens": 12}},
        ]
        events = [
            {"event": "api_stream_event", "request_id": "r", "frame": "data: " + json.dumps(c)}
            for c in chunks
        ]
        result = reconstruct_responses(events)["r"]
        self.assertEqual(12, result["usage"]["total_tokens"])
        choice = result["choices"][0]
        self.assertEqual("think more", choice["reasoning"])
        self.assertEqual("tool_calls", choice["finish_reason"])
        self.assertEqual({"path": "a"}, json.loads(choice["tool_calls"][0]["arguments"]))

    def test_cross_layer_audit_detects_missing_snapshots_and_finish_reason(self):
        folder = self.root / "traces/s"
        folder.mkdir(parents=True)
        arguments = {"calls": [{"name": "read_file", "arguments": {}}]}
        record = {
            "turn_number": 1,
            "origin": "model",
            "tool_calls": [],
            "execution_events": [],
            "final_answer": "done",
            "agent_termination": {"api_calls": 1},
        }
        for key in (
            "state_check",
            "process_check",
            "output_check",
            "workspace_after_agent",
            "mock_snapshot",
        ):
            record[key] = {}
        trace = [
            {"event": "turn_start"},
            {"event": "before_agent"},
            {"event": "tool_start", "id": "c", "name": "read_file", "arguments": {}},
            {"event": "tool_state_before", "id": "c", "workspace": {}, "mock_snapshot": {}},
            {"event": "tool_complete", "id": "c", "result": "data"},
            {"event": "tool_state_after", "id": "c", "workspace": {}, "mock_snapshot": {}},
            {"event": "agent_result", "result": {}},
            {"event": "turn_end", "record": record},
        ]
        context = {
            "request_id": "r",
            "scenario_id": "s",
            "run_id": "run",
            "turn_number": 1,
            "phase": "agent",
        }
        api = [
            {"event": "api_capture_start"},
            {"event": "api_request", **context, "sequence": 1, "body": {"messages": []}},
            {"event": "api_response_headers", **context, "status_code": 200},
            {
                "event": "api_response_body",
                **context,
                "body": {
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "content": "done",
                                "tool_calls": [
                                    {
                                        "id": "c",
                                        "function": {
                                            "name": "tool_call",
                                            "arguments": json.dumps(arguments),
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            },
            {"event": "api_response_end", **context},
            {"event": "api_capture_end"},
        ]
        report = {"reports": [{"id": "s", "api_requests": 1, "turns": [record]}]}
        report_path = self.root / "eval_report.json"
        report_path.write_text(json.dumps(report))
        original = report_path.read_bytes()

        def save():
            (folder / "api.jsonl").write_text("\n".join(json.dumps(e) for e in api))
            (folder / "turn_1.jsonl").write_text("\n".join(json.dumps(e) for e in trace))

        save()
        result = audit_run(self.root)
        self.assertTrue(result["passed"], result)
        self.assertEqual(1, result["counts"]["resolved_bridge_calls"])
        trace.insert(3, {"event": "native_control_start", "invocation_id": "g", "call_id": "c"})
        save()
        self.assertTrue(any("unpaired native" in e for e in audit_run(self.root)["errors"]))
        trace.pop(3)
        trace.pop(5)
        save()
        self.assertFalse(audit_run(self.root)["passed"])
        api[3]["body"]["choices"][0].pop("finish_reason")
        save()
        self.assertTrue(any("finish reason" in e for e in audit_run(self.root)["errors"]))
        self.assertEqual(original, report_path.read_bytes())

    def test_missing_payload_is_not_complete_evidence(self):
        path = self.root / "api.jsonl"
        events = [
            {"event": "api_capture_start"},
            {"event": "api_request", "request_id": "r", "body": {"messages": []}},
            {"event": "api_response_headers", "request_id": "r", "status_code": 200},
            {"event": "api_response_end", "request_id": "r"},
            {"event": "api_capture_end"},
        ]
        path.write_text("\n".join(json.dumps(e) for e in events))
        self.assertFalse(audit(path)["passed"])
        events.insert(3, {"event": "api_response_body", "request_id": "r", "body": {"choices": []}})
        path.write_text("\n".join(json.dumps(e) for e in events))
        self.assertTrue(audit(path)["passed"])
        with self.assertRaisesRegex(ValueError, "Credential"):
            audit(path, ["choices"])
