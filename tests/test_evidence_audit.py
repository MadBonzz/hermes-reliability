import json

from src.evaluation.audit_api_evidence import audit, reconstruct_responses
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
