"""Offline orchestration contracts; no provider or installed Hermes required."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from src.api_trace import SecretRedactor
from src.orchestrator import Client, Evidence, run, validate_analysis
from src.orchestrator_adapters import HermesAdapter
from src.orchestrator_hermes_worker import main as worker_main


class OrchestratorTests(unittest.TestCase):
    def test_saturation_requires_repeated_empty_reviewed_cycles(self):
        class EmptyClient:
            def __init__(self, *args):
                pass

            def ask(self, messages):
                task = json.loads(messages[1]["content"][0]["text"])
                if task["task"].startswith("Propose"):
                    return {
                        "scenarios": [],
                        "coverage": {"scope": "exhausted within supplied evidence"},
                        "unsupported_capabilities": [],
                        "stop_rationale": "No discriminating candidate",
                    }
                return {"accepted": True, "coverage_gaps": [], "issues": []}

        with tempfile.TemporaryDirectory() as tmp, patch("src.orchestrator.ROOT", Path(tmp)):
            config = {
                "scope": "limited",
                "orchestrator": {},
                "target": {},
                "adapter": {},
                "limits": {"cycles": 3},
            }
            output = run(config, EmptyClient, lambda c: HermesAdapter({}))
            state = json.loads((output / "campaign.json").read_text())
            self.assertEqual(len(state["cycles"]), 2)
            self.assertEqual(state["stop_reason"], "empirical_saturation_candidate_not_exhaustive")

    def test_generation_only_does_not_execute_and_preserves_inputs(self):
        case = {
            "id": "a",
            "name": "a",
            "property_tested": "a",
            "turns": [
                {
                    "turn_number": 1,
                    "prompt": "Summarize the input",
                    "state_checks": [],
                    "process_checks": [],
                    "output_checks": [],
                }
            ],
        }

        class PlanClient:
            def __init__(self, *args):
                pass

            def ask(self, messages):
                task = json.loads(messages[1]["content"][0]["text"])
                if task["task"].startswith("Propose"):
                    return {"scenarios": [case], "coverage": {}, "unsupported_capabilities": []}
                return {"accepted": True, "coverage_gaps": [], "issues": []}

        with tempfile.TemporaryDirectory() as tmp, patch("src.orchestrator.ROOT", Path(tmp)):
            source = Path(tmp) / "architecture.txt"
            source.write_text("original", encoding="utf-8")
            config = {
                "scope": "limited",
                "orchestrator": {},
                "target": {},
                "adapter": {},
                "documents": [str(source)],
            }
            with patch.object(HermesAdapter, "run", side_effect=AssertionError("must not execute")):
                output = run(config, PlanClient, lambda c: HermesAdapter({}))
            self.assertEqual(source.read_text(), "original")
            self.assertEqual(
                json.loads((output / "campaign.json").read_text())["stop_reason"], "generated_only"
            )
            self.assertTrue((output / "cycle_001/scenarios/a.json").is_file())

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

    def test_hermes_validation_rejects_escape_and_external_schema(self):
        adapter = HermesAdapter({})
        case = {
            "id": "a",
            "name": "a",
            "property_tested": "a",
            "turns": [
                {
                    "turn_number": 1,
                    "prompt": "Do task",
                    "state_checks": [],
                    "process_checks": [],
                    "output_checks": [],
                    "pre_turn_perturbations": [
                        {"type": "write_file", "path": "../bad", "content": "x"}
                    ],
                }
            ],
        }
        with self.assertRaises(ValueError):
            adapter.validate([case])
        case["$ref"] = "https://example.invalid/schema"
        with self.assertRaises(ValueError):
            adapter.validate([case])

    def test_complete_loop_reads_evidence_then_generates_deeper_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proposals = []

            class FakeAdapter:
                schema = {"type": "object"}

                def validate(self, cases):
                    proposals.extend(cases)

                def run(self, directory, target, key):
                    self_test.assertEqual(target["model"], "target-model")
                    p = directory / "report.json"
                    p.write_text('{"failed": true}', encoding="utf-8")
                    return [p]

            class FakeClient:
                def __init__(self, config, *args):
                    self_test.assertEqual(config["model"], "planner-model")
                    self.cycle = 0

                def ask(self, messages):
                    task = json.loads(messages[1]["content"][0]["text"])
                    if task["task"].startswith("Propose"):
                        self.cycle += 1
                        return {
                            "scenarios": [{"id": f"s{self.cycle}", "turns": [self.cycle]}],
                            "coverage": {},
                            "unsupported_capabilities": [],
                            "stop_rationale": "",
                        }
                    if task["task"].startswith("Independently"):
                        return {"accepted": True, "issues": [], "coverage_gaps": []}
                    if len(messages) == 2:
                        return {"read": [{"id": task["current_evidence_ids"][0]}]}
                    ref = json.loads(messages[-1]["content"])[0]["ref"]
                    return {
                        "scenarios": [
                            {
                                "id": f"s{self.cycle}",
                                "task_outcome": "failed",
                                "challenge_exposure": "reached",
                                "property_outcome": "observed_violation",
                                "evidence_refs": [ref],
                                "findings": [],
                                "deeper_tests": ["repeat"],
                            }
                        ],
                        "unresolved": [],
                        "common_patterns": [],
                    }

            self_test = self
            config = {
                "scope": "test",
                "orchestrator": {"model": "planner-model"},
                "target": {"model": "target-model"},
                "adapter": {},
                "limits": {"cycles": 2},
            }
            with patch("src.orchestrator.ROOT", root):
                output = run(config, FakeClient, lambda c: FakeAdapter(), execute=True)
            state = json.loads((output / "campaign.json").read_text())
            self.assertEqual(state["stop_reason"], "cycle_budget_exhausted")
            self.assertEqual(len(state["cycles"]), 2)
            self.assertEqual(len(proposals), 2)
            self.assertTrue((output / "cycle_002/analysis.json").is_file())

    def test_hermes_failed_gates_still_export_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = root / "request.json"
            output = root / "execution"
            output.mkdir()
            traces = root / "original_traces"
            traces.mkdir()
            (traces / "turn.jsonl").write_text("{}\n")
            (output / "eval_report.json").write_text(json.dumps({"trace_root": str(traces)}))
            request.write_text(
                json.dumps(
                    {
                        "output_directory": str(output),
                        "scenarios": "unused",
                        "target": {"base_url": "local", "model": "target"},
                    }
                )
            )
            result = root / "result.json"
            with (
                patch(
                    "sys.argv",
                    [
                        "worker",
                        "--orchestrator-request",
                        str(request),
                        "--orchestrator-result",
                        str(result),
                        "--hermes-root",
                        "unused",
                    ],
                ),
                patch("src.orchestrator_hermes_worker.eval_runner.main", return_value=1),
            ):
                self.assertEqual(worker_main(), 0)
            self.assertEqual(json.loads(result.read_text())["status"], "complete")
            self.assertTrue((traces / "turn.jsonl").exists())

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


if __name__ == "__main__":
    unittest.main()
