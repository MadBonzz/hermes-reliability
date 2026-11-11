"""Presentation provenance, caching and failure handling without provider calls."""

import copy
import json
import unittest

from jsonschema import ValidationError

from src.evaluation.runtime_support import write_json_atomic
from src.orchestrator.findings import (
    exclusive,
    generate,
    inputs,
    read_json,
    snapshot,
    source_records,
    validate_presentation,
)
from tests.helpers import TemporaryDirectoryTestCase


class FindingsTests(TemporaryDirectoryTestCase):
    def setUp(self):
        super().setUp()
        self.round = self.root / "round_001"
        self.analysis = {
            "scenarios": [
                {
                    "id": "payment",
                    "property_outcome": "observed_compliance",
                    "findings": [
                        {
                            "claim": "Model reused the key and service deduplicated the retry.",
                            "contributors": ["model", "mock_service"],
                            "certainty": "observed",
                            "evidence_refs": ["e1:0:100"],
                        }
                    ],
                }
            ],
            "unresolved": ["Other tool routes were not exercised."],
        }
        write_json_atomic(self.round / "analysis.json", self.analysis)
        write_json_atomic(
            self.root / "config.json",
            {
                "orchestrator": {
                    "model": "test",
                    "api_key_env": "TEST_KEY",
                    "base_url": "https://example.invalid/v1",
                }
            },
        )
        self.presentation = {
            "overview": "Recovery succeeded through the service.",
            "cards": [
                {
                    "title": "No duplicate payment",
                    "observation": "The retry reused the key.",
                    "causal_explanation": "The service deduplicated the repeated request.",
                    "practical_implication": "This recovery depended on both components.",
                    "limitations": ["Other routes were not tested."],
                    "source_ids": ["s1", "g1"],
                }
            ],
        }

    def test_contract_preserves_all_sources_without_invented_attribution(self):
        sources = source_records(self.analysis)
        validate_presentation(self.presentation, sources)
        self.assertEqual(["model", "mock_service"], sources["s1"]["contributors"])
        self.assertEqual(["e1:0:100"], sources["s1"]["evidence_refs"])
        for refs in (["s1"], ["s1", "g1", "invented"], ["s1", "g1", "s1"]):
            bad = copy.deepcopy(self.presentation)
            bad["cards"][0]["source_ids"] = refs
            with self.assertRaises((ValueError, ValidationError)):
                validate_presentation(bad, sources)
        bad = copy.deepcopy(self.presentation)
        bad["cards"][0]["contributors"] = ["hermes_runtime"]
        with self.assertRaises(ValidationError):
            validate_presentation(bad, sources)

    def test_generation_repairs_invalid_json_then_caches_and_logs_usage(self):
        calls = []
        good = self.presentation

        class Client:
            def __init__(self, config, directory, redactor, key, limit):
                self.directory = directory
                self.redactor = redactor

            def ask(self, messages):
                calls.append(copy.deepcopy(messages))
                number = len(calls)
                write_json_atomic(
                    self.directory / f"api_{number:04d}.request.json",
                    {
                        "messages": self.redactor(messages),
                    },
                )
                write_json_atomic(
                    self.directory / f"api_{number:04d}.response.json",
                    {
                        "elapsed_seconds": 1,
                        "body": json.dumps(
                            {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
                        ),
                    },
                )
                return {"overview": "Incomplete"} if number == 1 else good

        original = (self.round / "analysis.json").read_bytes()
        interrupted = inputs(self.round)[2] / "interrupted"
        write_json_atomic(
            interrupted / "api_0001.request.json",
            {
                "messages": [
                    {"role": "system", "content": ""},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({"task": "Present dashboard findings."}),
                            }
                        ],
                    },
                ],
            },
        )
        generate(self.round, {"TEST_KEY": "private"}, Client)
        result = snapshot(self.round)
        self.assertEqual("ready", result["status"])
        self.assertEqual(3, result["usage"]["requests"])
        self.assertEqual(["interrupted/api_0001.request.json"], result["usage"]["usage_missing"])
        self.assertEqual(20, result["usage"]["reported_tokens"]["input_tokens"])
        self.assertEqual(3, result["usage"]["by_role"]["dashboard_findings"]["requests"])
        self.assertIn("did not satisfy", calls[1][-1]["content"])
        generate(self.round, {"TEST_KEY": "private"}, Client)
        self.assertEqual(2, len(calls))
        self.assertEqual(original, (self.round / "analysis.json").read_bytes())
        self.analysis["unresolved"].append("New evidence gap")
        write_json_atomic(self.round / "analysis.json", self.analysis)
        self.assertEqual("pending", snapshot(self.round)["status"])

    def test_failures_stay_visible_and_do_not_loop_or_publish_invalid_cards(self):
        calls = []

        class Client:
            def __init__(self, *args):
                pass

            def ask(self, messages):
                calls.append(messages)
                raise RuntimeError("Cannot send private")

        generate(self.round, {"TEST_KEY": "private"}, Client)
        status = snapshot(self.round)
        self.assertEqual("error", status["status"])
        self.assertNotIn("private", status["error"])
        generate(self.round, {"TEST_KEY": "private"}, Client)
        self.assertEqual(1, len(calls))
        self.assertIsNone(read_json(inputs(self.round)[2] / "presentation.json"))

    def test_lock_prevents_a_second_worker_and_recovers_after_release(self):
        folder = inputs(self.round)[2]
        with exclusive(folder) as first:
            self.assertTrue(first)
            with exclusive(folder) as second:
                self.assertFalse(second)
        with exclusive(folder) as third:
            self.assertTrue(third)


if __name__ == "__main__":
    unittest.main()
