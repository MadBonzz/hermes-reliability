"""Version regression mode: differential comparison, results index, change capture and lanes."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.evaluation.differential import compare, markdown, report_summary
from src.orchestrator import regression_index
from src.orchestrator.quality import validate_version_plan
from src.orchestrator.runner import prepare_version_change, run

ROOT = Path(__file__).resolve().parent.parent


def phase(passed, tiers=True):
    turn = {f"{t}_check": {"passed": tiers} for t in ("state", "process", "output")}
    return {"status": "passed" if passed else "failed", "turns": [turn]}


def report(outcomes, commit=None):
    """outcomes: {scenario_id: [True/False/None per repetition]}; None = runtime error."""
    cases = []
    for sid, values in outcomes.items():
        for repetition, value in enumerate(values, 1):
            branch = {"status": "error", "turns": []} if value is None else phase(value)
            cases.append({"id": sid, "repetition": repetition, "branches": {"ordinary": branch}})
    data = {"cases": cases, "suite_sha256": "abc", "model": "m"}
    if commit:
        data["hermes"] = {"commit": commit, "dirty": False}
    return data


class DifferentialTests(unittest.TestCase):
    def test_verdicts_layers_and_invalid_phases(self):
        base = report({"a": [False, False], "b": [True, True], "c": [True, None]})
        target = report({"a": [True, True], "b": [True, False], "c": [True, True]})
        result = compare(base, target, {"a": "framework", "b": "task"})
        verdicts = {r["id"]: r["verdict"] for r in result["table"]}
        self.assertEqual({"a": "improved", "b": "regressed", "c": "unchanged"}, verdicts)
        self.assertEqual({"improved": 1}, result["verdicts_by_layer"]["framework"])
        self.assertEqual({"regressed": 1}, result["verdicts_by_layer"]["task"])
        self.assertEqual(1, result["invalid_phases"]["baseline"])
        self.assertIn("| a | ordinary | framework |", markdown(result))

    def test_missing_scenario_and_summary(self):
        result = compare(report({"a": [True]}), report({"b": [True]}))
        self.assertEqual(
            {"missing_in_candidate", "missing_in_baseline"},
            {r["verdict"] for r in result["table"]},
        )
        self.assertEqual(
            {"a/ordinary": {"runs": 1, "pass_rate": 1.0}}, report_summary(report({"a": [True]}))
        )
        self.assertEqual({}, report_summary({"status": "complete"}))


class IndexTests(unittest.TestCase):
    def test_record_lookup_prefix_and_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / "index.json"
            first = root / "runs/one/report.json"
            first.parent.mkdir(parents=True)
            first.write_text(json.dumps(report({"a": [True]}, commit="aaaa1111")))
            second = root / "runs/two/report.json"
            second.parent.mkdir(parents=True)
            second.write_text(json.dumps(report({"a": [False]}, commit="bbbb2222")))
            (root / "runs/three").mkdir()
            (root / "runs/three/report.json").write_text(json.dumps({"cases": []}))  # no commit
            self.assertEqual(2, len(regression_index.scan(index, root / "runs")))
            hits = regression_index.lookup(index, "aaaa")
            self.assertEqual([str(first.resolve())], [h["report"] for h in hits])
            self.assertEqual(1.0, hits[0]["summary"]["a/ordinary"]["pass_rate"])
            regression_index.scan(index, root / "runs")  # rescanning refreshes, no duplicates
            self.assertEqual(2, len(regression_index.load(index)["entries"]))
            self.assertEqual([], regression_index.lookup(index, ""))


class VersionPlanTests(unittest.TestCase):
    def row(self, **changes):
        row = {
            "id": "t1",
            "change_target": "tools/approval.py",
            "property_layer": "framework",
            "predicted_outcome": {"base": "fail", "target": "pass", "reason": "new gate"},
        }
        row.update(changes)
        return row

    def test_accepts_complete_rows_and_rejects_gaps(self):
        changed = ["tools/approval.py"]
        validate_version_plan(
            {"rationale": [self.row(), self.row(change_target="control")]}, changed
        )
        for bad in (
            self.row(change_target="unchanged.py"),
            self.row(property_layer="model"),
            self.row(predicted_outcome={"base": "fail"}),
            self.row(predicted_outcome={"base": "fail", "target": "pass", "reason": ""}),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_version_plan({"rationale": [bad]}, changed)
        with self.assertRaises(ValueError):  # controls only
            validate_version_plan({"rationale": [self.row(change_target="control")]}, changed)


@unittest.skipUnless(shutil.which("git"), "git is required")
class ChangeCaptureTests(unittest.TestCase):
    def git(self, *args, cwd):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_records_changed_files_diff_and_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upstream = root / "upstream"
            upstream.mkdir()
            ident = ["-c", "user.email=t@example.com", "-c", "user.name=t"]
            self.git("init", "-q", cwd=upstream)
            self.git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=upstream)
            (upstream / "run_agent.py").write_text("v1\n")
            (upstream / "tools.py").write_text("same\n")
            self.git("add", ".", cwd=upstream)
            self.git(*ident, "commit", "-qm", "base", cwd=upstream)
            base = self.git("rev-parse", "HEAD", cwd=upstream)
            (upstream / "run_agent.py").write_text("v2 approval gate\n")
            self.git(*ident, "commit", "-qam", "add approval gate", cwd=upstream)
            target = self.git("rev-parse", "HEAD", cwd=upstream)
            repository = upstream.resolve().as_uri()
            source = root / "target_checkout"
            self.git("clone", "-q", repository, str(source), cwd=root)
            self.git("checkout", "-q", "--detach", target, cwd=source)
            output = root / "campaign"
            output.mkdir()
            config = {
                "source": {
                    "repository": repository,
                    "revision": target,
                    "base_revision": base,
                    "base_local_path": str(root / "base_checkout"),
                }
            }
            base_source, change = prepare_version_change(config, output, source)
            self.assertEqual("v1\n", (base_source / "run_agent.py").read_text())
            self.assertEqual(["run_agent.py"], change["changed_files"])
            self.assertIn("add approval gate", change["commit_log"])
            self.assertIn("+v2 approval gate", (output / "version_change.diff").read_text())
            self.assertEqual(change, json.loads((output / "version_change.json").read_text()))
            same = {"source": {**config["source"], "base_revision": target}}
            with self.assertRaises(ValueError):
                prepare_version_change(same, output, source)


class VersionRunTests(unittest.TestCase):
    def test_base_lane_runs_on_base_revision_and_differential_is_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src/evaluation").mkdir(parents=True)
            (root / "src/__init__.py").write_text("")
            (root / "src/evaluation/evaluate.py").write_text("VERSION = 'original'\n")
            (root / "scenario.schema.json").write_bytes(
                (ROOT / "scenario.schema.json").read_bytes()
            )
            source, base_source = root / "source", root / "base_source"
            for path in (source, base_source):
                path.mkdir()
                (path / "run_agent.py").write_text("# agent")
            adapters, executions = [], []
            change = {
                "base_revision": "b" * 40,
                "target_revision": "t" * 40,
                "changed_files": ["run_agent.py"],
            }

            class Adapter:
                schema = json.loads((ROOT / "scenario.schema.json").read_text())

                def __init__(self, config):
                    self.root = config["hermes_root"]
                    adapters.append(config)

                def preflight(self, *args):
                    return {"passed": True}

                def execute_revision(self, evaluator, output, *args):
                    on_base = self.root == str(base_source)
                    executions.append((output.parent.name, on_base))
                    output.mkdir()
                    data = report({"task_1": [not on_base]})
                    (output / "report.json").write_text(json.dumps(data))
                    return {"status": "complete"}

            class Planner:
                def __init__(self, *args):
                    pass

                def ask(self, messages):
                    task = json.loads(messages[1]["content"][0]["text"])
                    if task["task"].startswith("Generate"):
                        assert task["version_change"]["changed_files"] == ["run_agent.py"]
                        if len(messages) == 2:
                            return {"source": [{"path": "run_agent.py"}]}
                        if len(messages) == 4:
                            item = json.loads(json.dumps(CASE))
                            return {
                                "actions": [
                                    {
                                        "op": "write",
                                        "path": "scenarios/task_1.json",
                                        "content": json.dumps(item),
                                    }
                                ]
                            }
                        ref = json.loads(messages[3]["content"])[0]["ref"]
                        return {
                            "ready": True,
                            "rationale": [
                                {
                                    "id": "task_1",
                                    "architecture_refs": [ref],
                                    "change_target": "run_agent.py",
                                    "property_layer": "framework",
                                    "predicted_outcome": {
                                        "base": "fail",
                                        "target": "pass",
                                        "reason": "the change adds a guard",
                                    },
                                }
                            ],
                        }
                    assert task["version_differential"]["available"]
                    if len(messages) == 2:
                        return {"read": [{"id": next(iter(task["raw_evidence"]))}]}
                    ref = json.loads(messages[-1]["content"])[0]["ref"]
                    return {
                        "scenarios": [
                            {
                                "id": "task_1",
                                "property_outcome": "uncertain",
                                "evidence_refs": [ref],
                                "findings": [],
                            }
                        ],
                        "unresolved": [],
                        "converged": False,
                        "convergence_reason": "One round only",
                        "proposed_changes": ["repeat"],
                    }

            def prepare(config, output):
                (output / "agent_source.json").write_text('{"files":["run_agent.py"]}')
                return source

            def prepare_change(config, output, src):
                (output / "version_change.json").write_text(json.dumps(change))
                (output / "version_change.diff").write_text("+guard\n")
                return base_source, change

            index = root / "eval_harness/regression_index.json"
            config = {
                "scope": "test",
                "source": {"base_revision": change["base_revision"]},
                "orchestrator": {},
                "target": {},
                "adapter": {},
                "limits": {"rounds": 1, "generations": 1, "initial_tests": 1, "active_tests": 1},
            }
            with (
                patch("src.orchestrator.runner.ROOT", root),
                patch("src.orchestrator.runner.prepare_source", side_effect=prepare),
                patch("src.orchestrator.runner.prepare_version_change", side_effect=prepare_change),
                # Synthetic reports omit audit evidence; the audit itself is tested elsewhere.
                patch(
                    "src.orchestrator.runner.audit_campaign",
                    return_value={
                        "passed": True,
                        "errors": [],
                        "phases": [],
                        "cost_recomputed_once_per_phase": {},
                    },
                ),
            ):
                output = run(config, Planner, Adapter, execute=True)
            self.assertEqual({str(source), str(base_source)}, {a["hermes_root"] for a in adapters})
            base_config = next(a for a in adapters if a["hermes_root"] == str(base_source))
            self.assertEqual(change["base_revision"], base_config["source_revision"])
            self.assertEqual(
                [("candidate", False), ("trusted", False), ("base_version", True)], executions
            )
            differential = json.loads((output / "round_001/version_differential.json").read_text())
            self.assertEqual({"improved": 1}, differential["verdicts"])
            self.assertEqual("framework", differential["table"][0]["layer"])
            self.assertEqual("fail", differential["table"][0]["predicted_outcome"]["base"])
            entries = regression_index.load(index)["entries"]
            self.assertEqual(
                {(change["target_revision"], "trusted"), (change["base_revision"], "base_version")},
                {(e["revision"], e["lane"]) for e in entries},
            )
            state = json.loads((output / "campaign.json").read_text())
            self.assertEqual(["run_agent.py"], state["version_change"]["changed_files"])

    def test_native_runtimes_are_rejected_in_version_mode(self):
        config = {
            "scope": "x",
            "source": {"base_revision": "b"},
            "orchestrator": {},
            "target": {},
            "adapter": {"kind": "openhands"},
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("src.orchestrator.runner.prepare_source", return_value=Path(tmp)),
        ):
            config["artifacts_root"] = tmp
            with self.assertRaises(ValueError):
                run(config, None, None)


CASE = {
    "benchmark_version": 2,
    "id": "task_1",
    "name": "A",
    "property_tested": "integrity",
    "complexity": {
        "family": "integrity",
        "turns": 1,
        "input_files": 0,
        "services": 0,
        "records": 0,
        "dependency_depth": 1,
        "fault_types": [],
        "interaction": "answer",
    },
    "turns": [
        {
            "turn_number": 1,
            "prompt": "What is two plus two?",
            "state_checks": [],
            "process_checks": [],
            "output_checks": [{"type": "contains_all", "values": ["4"]}],
        }
    ],
}


if __name__ == "__main__":
    unittest.main()
