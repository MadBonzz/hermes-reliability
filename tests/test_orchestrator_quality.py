"""Response bookkeeping and editing boundaries; no semantic-quality claims."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.orchestrator.prompts import VARIANTS, variant_context
from src.orchestrator.quality import (
    check_catalog,
    validate_assessments,
    validate_design,
    validate_stopping,
)
from src.orchestrator.runner import CampaignPreempted, Evidence, check_stop, exchange
from src.orchestrator.workspace import Workspace
from tests.test_orchestrator import ROOT, case


class QualityTests(unittest.TestCase):
    def test_evidence_accepts_returned_citations_without_bypassing_integrity(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.txt"
            path.write_text("0123456789", encoding="utf-8")
            evidence = Evidence(lambda value: value)
            identity = evidence.add(path)
            first = evidence.read({"id": identity, "offset": 3, "length": 4})
            self.assertEqual(evidence.read({"id": first["ref"]}), first)
            explicit = evidence.read({"id": first["ref"], "offset": 0, "length": 2})
            self.assertEqual(explicit["text"], "01")
            self.assertEqual(explicit["ref"], f"{identity}:0:2")
            with self.assertRaisesRegex(ValueError, "Unknown evidence"):
                evidence.read({"id": f"{identity}:8:10"})
            path.write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed since indexing"):
                evidence.read({"id": first["ref"]})

    def test_source_search_discovers_paths_without_citation_credit(self):
        replies = iter(
            [
                {"source": [{"query": "MIDDLEWARE", "offset": 1, "limit": 1}]},
                {"source": [{"query": "middleware", "offset": 0, "limit": 1}]},
                {"source": [{"query": "middleware", "offset": -1}]},
                {"done": True},
            ]
        )
        messages = []
        evidence = Evidence(lambda value: value)
        with patch("src.orchestrator.runner.source_files") as inventory:
            inventory.return_value = ["a/middleware.py", "b/middleware.py", "other.py"]
            exchange(
                SimpleNamespace(ask=lambda _: next(replies)),
                evidence,
                {},
                source=ROOT,
                conversation=messages,
            )
        inventory.assert_called_once_with(ROOT)
        results = [
            json.loads(m["content"])
            for m in messages
            if m["role"] == "user" and isinstance(m["content"], str)
        ]
        self.assertEqual(results[0][0]["paths"], ["b/middleware.py"])
        self.assertEqual(results[0][0]["total"], 2)
        self.assertFalse(results[0][0]["has_more"])
        self.assertTrue(results[1][0]["has_more"])
        self.assertIn("error", results[2][0])
        self.assertFalse(evidence.items)
        self.assertFalse(evidence.seen)

    def design(self):
        spec = case()
        spec["turns"][0]["state_checks"] = [{"type": "file_exists", "path": "answer.txt"}]
        row = {
            key: "Unrestricted explanation " * 1000
            for key in (
                "invariant",
                "exposure",
                "architecture_route",
                "complexity_reason",
                "control_reason",
            )
        }
        row.update(
            id=spec["id"],
            contract_checks=[
                {
                    "check_ids": list(check_catalog([spec])[spec["id"]]),
                    "visible_requirement": "User requests answer.txt.",
                    "valid_alternative": "Any valid way of producing the requested artifact.",
                    "invalid_example": "Claim completion without producing the file.",
                }
            ],
        )
        return spec, {"rationale": [{"id": spec["id"]}], "design_evidence": [row]}

    def test_structural_coverage_accepts_long_freeform_explanations(self):
        spec, plan = self.design()
        validate_design(plan, [spec])
        for mutation in ("missing", "duplicate", "invented", "unknown_scenario"):
            bad = copy.deepcopy(plan)
            refs = bad["design_evidence"][0]["contract_checks"][0]["check_ids"]
            if mutation == "missing":
                refs.pop()
            elif mutation == "duplicate":
                refs.append(refs[0])
            elif mutation == "invented":
                refs[0] = "ordinary:99:state:0"
            else:
                bad["rationale"][0]["id"] = "unknown"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_design(bad, [spec])

    def test_catalog_uses_condition_specific_checks(self):
        spec, _ = self.design()
        spec["experiment"] = {
            "branch_turn": 1,
            "baseline_turns": [
                {
                    "turn_number": 1,
                    "state_checks": [{"type": "file_exists", "path": "baseline.txt"}],
                }
            ],
        }
        catalog = check_catalog([spec])[spec["id"]]
        self.assertEqual(catalog["baseline:1:state:0"]["path"], "baseline.txt")
        self.assertEqual(catalog["treatment:1:state:0"]["path"], "answer.txt")

    def test_review_and_stopping_cannot_contradict_their_fields(self):
        spec = case()
        review = {
            "accepted": True,
            "assessments": [
                {
                    "id": spec["id"],
                    "verdict": "accept",
                    "reason": "Evidence supports this assessment.",
                }
            ],
        }
        validate_assessments(review, [spec])
        review["assessments"][0]["verdict"] = "unresolved"
        with self.assertRaises(ValueError):
            validate_assessments(review, [spec])
        for field in ("unresolved", "proposed_changes"):
            with self.assertRaises(ValueError):
                validate_stopping(
                    {
                        "converged": True,
                        "convergence_reason": "Done",
                        "unresolved": [],
                        "proposed_changes": [],
                        field: ["Material gap"],
                    }
                )
        result = {
            "converged": False,
            "convergence_reason": "More evidence needed",
            "unresolved": ["Sample variance is unknown"],
            "proposed_changes": [],
        }
        with self.assertRaisesRegex(ValueError, "unchanged_suite_reason"):
            validate_stopping(result)
        result["unchanged_suite_reason"] = "Repeat the same task to estimate sampling variability."
        validate_stopping(result)
        result.pop("unchanged_suite_reason")
        result["proposed_changes"] = ["Add a missing oracle counterexample"]
        validate_stopping(result)
        result["converged"] = "false"
        with self.assertRaisesRegex(ValueError, "boolean"):
            validate_stopping(result)

    def test_edit_protection_preserves_read_access_and_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Workspace(Path(temporary) / "working", ROOT)
            workspace.action(
                {"op": "write", "path": "scenarios/task_a.json", "content": json.dumps(case())}
            )
            self.assertTrue(workspace.action({"op": "validate"}, readonly=True)["valid"])
            data = workspace.action({"op": "read", "path": "src/evaluation/audit.py"})
            with self.assertRaisesRegex(ValueError, "read-only"):
                workspace.action(
                    {
                        "op": "delete",
                        "path": "src/evaluation/audit.py",
                        "expected_sha256": data["sha256"],
                    }
                )
            workspace.active_limit = 0
            with self.assertRaises(ValueError):
                workspace.action({"op": "validate"})

    def test_probe_selection_prevents_unrelated_checks_masking_artifact_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Workspace(Path(temporary) / "working", ROOT)
            spec = case()
            spec["turns"][0].update(
                state_checks=[
                    {"type": "file_exact_lines", "path": "answer.txt", "expected": ["7"]}
                ],
                process_checks=[{"type": "tool_called", "tool_name": "write_file"}],
                output_checks=[],
            )
            workspace.action(
                {"op": "write", "path": "scenarios/task_a.json", "content": json.dumps(spec)}
            )
            request = {
                "op": "probe",
                "path": "scenarios/task_a.json",
                "turn_number": 1,
                "expect_pass": True,
                "reason": "Valid artifact, no synthetic call records",
                "files": {"answer.txt": "7\n"},
            }
            self.assertFalse(workspace.action(request)["passed"])
            selected = ["ordinary:1:state:0"]
            result = workspace.action({**request, "check_ids": selected})
            self.assertTrue(result["passed"])
            self.assertEqual(result["selected_check_ids"], selected)
            self.assertEqual(result["unselected_check_ids"], ["ordinary:1:process:0"])
            result = workspace.action(
                {
                    **request,
                    "check_ids": selected,
                    "files": {"answer.txt": "99\n"},
                    "expect_pass": False,
                }
            )
            self.assertFalse(result["passed"])
            self.assertTrue(result["matches_expectation"])
            for invalid in (
                [],
                ["baseline:1:state:0"],
                ["ordinary:2:state:0"],
                selected * 2,
                "ordinary:1:state:0",
            ):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    workspace.action({**request, "check_ids": invalid})

    def test_probe_native_output_root_is_distinct_and_profile_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Workspace(Path(temporary) / "working", ROOT)
            spec = case()
            spec["turns"][0].update(
                state_checks=[
                    {
                        "type": "file_exact_lines",
                        "path": "report.txt",
                        "expected": ["correct"],
                        "artifact_scope": "outputs",
                    }
                ],
                process_checks=[],
                output_checks=[],
            )
            workspace.action(
                {"op": "write", "path": "scenarios/task_a.json", "content": json.dumps(spec)}
            )
            request = {
                "op": "probe",
                "path": "scenarios/task_a.json",
                "turn_number": 1,
                "expect_pass": True,
                "reason": "Correct native deliverable",
                "files": {"report.txt": "wrong"},
                "output_files": {"report.txt": "correct"},
            }
            with self.assertRaisesRegex(ValueError, "DeerFlow"):
                workspace.action(request)
            workspace.runtime_kind = "deerflow"
            self.assertTrue(workspace.action(request)["passed"])
            self.assertFalse(workspace.action({**request, "output_files": {}})["passed"])
            with self.assertRaises(ValueError):
                workspace.action(
                    {**request, "output_files": {"../workspace/report.txt": "correct"}}
                )

    def test_stop_and_prompt_variants_are_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            check_stop(temporary)
            (Path(temporary) / "stop.json").write_text(
                json.dumps({"reason": "Observed oracle defects; revise before more spending"})
            )
            with self.assertRaisesRegex(CampaignPreempted, "oracle defects"):
                check_stop(temporary)
        for name in VARIANTS:
            context = variant_context(name, "main")
            if name != "baseline":
                self.assertEqual(context["prompt_variant"], name)
                self.assertTrue(context["worked_examples"])

    @unittest.skipUnless(
        os.environ.get("NATIVE_AGENT_TEST_IMAGE"), "requires explicit native image"
    )
    def test_isolated_probe_rejects_wrong_code_and_accepts_valid_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Workspace(Path(temporary) / "working", ROOT)
            workspace.artifact_runtime = {
                "image": os.environ["NATIVE_AGENT_TEST_IMAGE"],
                "workspace": "/workspace",
                "visible_workspace": "/workspace",
            }
            spec = case()
            spec["turns"][0].update(
                output_checks=[],
                state_checks=[
                    {
                        "type": "artifact_python",
                        "name": "executable_behavior",
                        "timeout_seconds": 10,
                        "python": "import runpy\nassert runpy.run_path('calc.py')['answer']() == 7\n",
                    }
                ],
            )
            workspace.action(
                {"op": "write", "path": "scenarios/task_a.json", "content": json.dumps(spec)}
            )
            before = workspace.inventory()
            for content, expected in [
                ("def answer(): return 99\n", False),
                ("def answer(): return 7\n", True),
            ]:
                result = workspace.action(
                    {
                        "op": "probe",
                        "path": "scenarios/task_a.json",
                        "turn_number": 1,
                        "expect_pass": expected,
                        "reason": "Check behavior, not function-name keywords",
                        "files": {"calc.py": content},
                    }
                )
                self.assertEqual(result["passed"], expected)
                self.assertTrue(result["matches_expectation"])
                self.assertEqual(len(result["artifact_checks"]), 1)
                self.assertTrue(Path(result["artifact_checks"][0]["stderr"]).exists())
            self.assertEqual(before, workspace.inventory())
            checker = workspace.root / "src/evaluation/artifact_checks.py"
            checker.write_text(
                checker.read_text(encoding="utf-8") + "\n# revision\n", encoding="utf-8"
            )
            result = workspace.action(
                {
                    "op": "probe",
                    "path": "scenarios/task_a.json",
                    "turn_number": 1,
                    "expect_pass": True,
                    "reason": "Verify checker identity coverage",
                    "files": {"calc.py": "def answer(): return 7\n"},
                }
            )
            self.assertTrue(result["passed"])
            self.assertFalse(result["candidate_checker_matches"])
            self.assertFalse(
                result["checker_evidence"]["src/evaluation/artifact_checks.py"]["matches"]
            )


if __name__ == "__main__":
    unittest.main()
