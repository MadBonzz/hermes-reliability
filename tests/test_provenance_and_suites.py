"""Hermes revision provenance, approval mode and frozen suite files."""

import json
import os
import shutil
import subprocess
import unittest
from unittest.mock import patch

from src.evaluation import evaluate, utils
from src.evaluation.runtime_support import hermes_provenance
from tests.helpers import TemporaryDirectoryTestCase, scenario


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@unittest.skipUnless(shutil.which("git"), "git is required")
class HermesProvenanceTests(TemporaryDirectoryTestCase):
    def test_missing_checkout_records_unknown_values(self):
        self.assertEqual(
            {"path": None, "commit": None, "describe": None, "dirty": None},
            hermes_provenance(None),
        )

    def test_non_git_directory_records_path_without_revision(self):
        record = hermes_provenance(self.root)
        self.assertEqual(str(self.root), record["path"])
        self.assertIsNone(record["commit"])
        self.assertIsNone(record["dirty"])

    def test_commit_and_dirty_state_are_recorded(self):
        repo = self.root / "hermes"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "run_agent.py").write_text("# agent\n")
        _git(repo, "add", "run_agent.py")
        _git(
            repo,
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "commit",
            "-q",
            "-m",
            "init",
        )
        clean = hermes_provenance(repo)
        self.assertEqual(40, len(clean["commit"]))
        self.assertFalse(clean["dirty"])
        (repo / "run_agent.py").write_text("# edited\n")
        self.assertTrue(hermes_provenance(repo)["dirty"])


class ApprovalModeTests(unittest.TestCase):
    def test_auto_sets_auto_approve_and_gate_removes_it(self):
        with patch.dict(os.environ, {}, clear=False):
            utils.apply_hermes_approval("auto")
            self.assertEqual("1", os.environ["HERMES_AUTO_APPROVE"])
            self.assertEqual("1", os.environ["HERMES_NON_INTERACTIVE"])
            utils.apply_hermes_approval("gate")
            self.assertNotIn("HERMES_AUTO_APPROVE", os.environ)
            self.assertEqual("1", os.environ["HERMES_NON_INTERACTIVE"])

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            utils.apply_hermes_approval("yes")


class SuiteFileTests(TemporaryDirectoryTestCase):
    def _project(self):
        project = self.root / "project"
        (project / "scenarios").mkdir(parents=True)
        (project / "src/evaluation").mkdir(parents=True)
        (project / "src/__init__.py").write_text("")
        shutil.copy2(evaluate.ROOT / "scenario.schema.json", project / "scenario.schema.json")
        (project / "src/evaluation/eval_worker.py").write_text("# worker\n")
        for name in ("first", "second", "third"):
            (project / "scenarios" / f"{name}.json").write_text(json.dumps(scenario(name)))
        return project

    def test_suite_file_selects_scenarios_and_is_recorded(self):
        project = self._project()
        suite = self.root / "core.json"
        suite.write_text(json.dumps({"name": "core_test", "scenario_ids": ["second", "third"]}))
        seen = []

        def execute(spec, config, key, **kwargs):
            seen.append((spec["id"], config["hermes_approval"]))
            return {"id": spec["id"], "status": "passed"}

        output = self.root / "run"
        with (
            patch.object(evaluate, "ROOT", project),
            patch.object(evaluate, "run_case", side_effect=execute),
            patch("src.evaluation.analyze.analyze"),
        ):
            code = evaluate.main(
                ["--output", str(output), "--suite-file", str(suite), "--hermes-approval", "gate"]
            )
        self.assertEqual(0, code)
        self.assertEqual([("second", "gate"), ("third", "gate")], seen)
        report = json.loads((output / "report.json").read_text())
        self.assertEqual("gate", report["hermes_approval"])
        self.assertEqual("core_test", report["suite_file"]["name"])
        self.assertEqual(64, len(report["suite_file"]["sha256"]))
        self.assertEqual({"path", "commit", "describe", "dirty"}, set(report["hermes"]))

    def test_unknown_suite_id_fails_before_creating_output(self):
        project = self._project()
        suite = self.root / "core.json"
        suite.write_text(json.dumps({"scenario_ids": ["missing"]}))
        output = self.root / "run"
        with patch.object(evaluate, "ROOT", project), self.assertRaises(SystemExit) as stopped:
            evaluate.main(["--output", str(output), "--suite-file", str(suite)])
        self.assertEqual(2, stopped.exception.code)
        self.assertFalse(output.exists())

    def test_repository_core_suite_lists_existing_scenarios(self):
        suite = json.loads((evaluate.ROOT / "suites/core_v1.json").read_text())
        names = {p.stem for p in (evaluate.ROOT / "scenarios").glob("*.json")}
        self.assertEqual(len(suite["scenario_ids"]), len(set(suite["scenario_ids"])))
        self.assertLessEqual(set(suite["scenario_ids"]), names)
