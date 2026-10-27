import json
import unittest

from src.correlate_lmstudio_logs import match_request, read_requests


class LogCorrelationTests(unittest.TestCase):
    def test_logs_match_truncated_prompt_but_preserve_run_ambiguity(self):
        text = "Create a uniquely named file in this workspace with content hello world"
        entries = [
            dict(model="model", prompt=text, report=str(i), scenario_id="s", turn_number=1)
            for i in (1, 2)
        ]
        result = match_request(
            {
                "model": "model",
                "messages": [
                    {
                        "role": "user",
                        "content": "Create a uniquely named file ... <Truncated in logs> ... with content hello world",
                    }
                ],
            },
            entries,
        )
        self.assertEqual("ambiguous", result["status"])
        self.assertEqual(2, len(result["candidates"]))

    def test_logs_use_latest_user_and_explicit_marker(self):
        body = {
            "messages": [
                {"role": "user", "content": "[EVAL run=x scenario=s turn=1]"},
                {"role": "user", "content": "[EVAL run=x scenario=s turn=2]"},
            ]
        }
        self.assertEqual(2, match_request(body, [])["turn_number"])
        text = (
            "[2026-10-18 00:00:00][DEBUG] Received request: POST to /v1/chat/completions with body "
            + json.dumps(body)
        )
        self.assertEqual(body, list(read_requests(text))[0]["body"])

    def test_log_finalization_request_uses_preceding_task(self):
        body = {
            "model": "x",
            "messages": [
                {"role": "user", "content": "[EVAL run=r scenario=s turn=1]\nTask"},
                {
                    "role": "user",
                    "content": "You've reached the maximum number of tool-calling iterations",
                },
            ],
        }
        match = match_request(body, [])
        self.assertEqual("r", match["run_id"])
        self.assertEqual("forced_final_or_empty_response_recovery", match["request_kind"])
