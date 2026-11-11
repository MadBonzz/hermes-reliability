"""Dashboard lifecycle, source selection and evidence presentation without model calls."""

import json
import subprocess
import unittest
from unittest.mock import patch

from src.evaluation.runtime_support import sha256_file, write_json_atomic
from src.orchestrator.dashboard import Catalog, identity
from src.orchestrator.runner import run
from src.orchestrator.workspace import Workspace
from src.plugin import source as app
from tests.helpers import TemporaryDirectoryTestCase
from tests.host_safety import HostSafetyMixin
from tests.test_orchestrator import ROOT, case


class DashboardTests(HostSafetyMixin, TemporaryDirectoryTestCase):
    def test_local_campaign_replaces_its_bundled_copy_in_sidebar(self):
        path = self.campaign()
        row = Catalog(self.root).cards()[0]
        write_json_atomic(
            self.root / "examples/hermes.json",
            {
                "format_version": 1,
                "card": {**row, "id": "example_hermes"},
                "detail": {"id": "example_hermes"},
            },
        )
        cards = Catalog(self.root, self.root / "examples").cards()
        self.assertEqual([identity(path)], [r["id"] for r in cards])

    def campaign(self, root=None):
        root = root or self.root
        path = root / "cohort/runs/hermes_one/attempts/one"
        path.mkdir(parents=True)
        write_json_atomic(
            path / "config.json",
            {
                "source": {"repository": "https://example.com/agent.git", "revision": "a" * 40},
                "adapter": {"kind": "hermes"},
                "orchestrator": {"model": "wonder"},
                "target": {"model": "spark"},
            },
        )
        write_json_atomic(path / "campaign.json", {"rounds": [], "stop_reason": "error"})
        write_json_atomic(path / "progress.json", {"phase": "generation", "round": 1})
        return path

    def test_catalog_reads_active_campaign_without_claiming_placeholder_error(self):
        path = self.campaign()
        catalog = Catalog(self.root)
        rows = catalog.cards()
        self.assertEqual(1, len(rows))
        self.assertIsNone(rows[0]["stop_reason"])
        write_json_atomic(path / "working/scenarios/task.json", case())
        journal = path / "round_001/generation_01/actions.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            json.dumps({"request": {"source": [{"path": "tools.py"}]}}) + '\n{"partial":',
            encoding="utf-8",
        )
        detail = catalog.detail(rows[0]["id"])
        self.assertEqual(1, len(detail["tests"]))
        self.assertEqual("tools.py", detail["activity"][0]["subject"])
        self.assertEqual([], detail["rounds"][0]["generations"][0]["reviews"])
        write_json_atomic(
            path / "round_001/trusted/execution/report.json",
            {
                "status": "complete",
                "cases": [
                    {
                        "id": "task",
                        "status": "complete",
                        "branches": {
                            "baseline": {"status": "passed"},
                            "treatment": {"status": "failed"},
                        },
                    }
                ],
            },
        )
        lane = catalog.detail(rows[0]["id"])["rounds"][0]["lanes"]["trusted"]
        self.assertEqual((1, 2), (lane["passed"], lane["total"]))
        with self.assertRaises(ValueError):
            catalog.resolve("../../.env")

    def test_seed_checks_hashes_and_uses_current_validation(self):
        path = self.campaign()
        stage = path / "round_001/generation_01"
        source = stage / "candidate/scenarios/task_a.json"
        write_json_atomic(source, case())
        write_json_atomic(stage / "hashes.json", {"scenarios/task_a.json": sha256_file(source)})
        prior_analysis = path / "round_001/analysis.json"
        write_json_atomic(prior_analysis, {"converged": False})
        catalog = Catalog(self.root)
        selection = catalog.seed_selection(identity(path))
        self.assertEqual(sha256_file(prior_analysis), selection["evidence"][str(prior_analysis)])
        workspace = Workspace(self.root / "working", ROOT)
        result = workspace.seed(selection, 12)
        self.assertEqual(1, result["count"])
        self.assertEqual(
            source.read_bytes(), (workspace.root / "scenarios/task_a.json").read_bytes()
        )
        source.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "content changed"):
            workspace.seed(selection, 12)
        with self.assertRaisesRegex(ValueError, "no verified"):
            catalog.seed_selection(identity(path))

    def test_sidebar_keeps_failed_stopped_and_incomplete_runs_for_inspection(self):
        path = self.campaign()
        catalog = Catalog(self.root)
        self.assertEqual(1, len(catalog.cards()))
        write_json_atomic(path / "progress.json", {"phase": "terminal"})
        write_json_atomic(
            path / "round_001/analysis.json",
            {
                "scenarios": [{"id": "task", "findings": [{"claim": "Observed"}]}],
            },
        )
        for reason in ("error", "operator_preempted", "invalid_final_scenario_contract", None):
            write_json_atomic(path / "campaign.json", {"stop_reason": reason})
            self.assertEqual(1, len(catalog.cards()))
        write_json_atomic(path / "campaign.json", {"stop_reason": "execution_round_limit"})
        self.assertEqual(1, len(catalog.cards()))
        (path / "round_002").mkdir()
        self.assertEqual(1, len(catalog.cards()))
        self.assertTrue((path / "round_001/analysis.json").exists())

    def test_local_source_is_cloned_and_original_checkout_is_preserved(self):
        source = self.root / "source"
        source.mkdir()

        def git(*args):
            return subprocess.check_output(
                ["git", "-C", str(source), *args], text=True, stderr=subprocess.PIPE
            ).strip()

        git("init", "-q")
        (source / "run_agent.py").write_text("# version one\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=t@example.com", "commit", "-qm", "one")
        base = git("rev-parse", "HEAD")
        (source / "run_agent.py").write_text("# version two\n")
        git("-c", "user.name=Test", "-c", "user.email=t@example.com", "commit", "-qam", "two")
        target = git("rev-parse", "HEAD")
        folder = self.root / "launch"
        folder.mkdir()
        config = app.resolve_source(
            {
                "source_input": str(source),
                "adapter": {"kind": "hermes"},
                "previous_source": {"repository": source.as_uri(), "revision": base},
            },
            folder,
        )
        self.assertEqual(target, config["source"]["revision"])
        self.assertEqual(base, config["source"]["base_revision"])
        self.assertEqual(target, git("rev-parse", "HEAD"))
        self.assertEqual("# version one\n", (folder / "agent_source_base/run_agent.py").read_text())
        self.assertEqual("# version two\n", (folder / "agent_source/run_agent.py").read_text())

    def test_seeded_campaign_receives_prior_evidence_and_active_suite_budget(self):
        source = self.root / "source"
        source.mkdir()
        (source / "run_agent.py").write_text("# source")
        seeds = self.root / "seeds"
        hashes = {}
        for name in ("task_a", "task_b"):
            item = {**case(), "id": name}
            path = seeds / f"{name}.json"
            write_json_atomic(path, item)
            hashes[path.name] = sha256_file(path)
        prior = self.root / "analysis.json"
        write_json_atomic(prior, {"converged": False})
        observed = []

        class Planner:
            def __init__(self, *args):
                pass

            def ask(self, messages):
                task = json.loads(messages[1]["content"][0]["text"])
                if len(messages) == 2:
                    observed.append(task)
                    return {"source": [{"path": "run_agent.py"}]}
                ref = json.loads(messages[3]["content"])[0]["ref"]
                return {
                    "ready": True,
                    "rationale": [
                        {"id": name, "architecture_refs": [ref]} for name in ("task_a", "task_b")
                    ],
                }

        class Adapter:
            schema = json.loads((ROOT / "scenario.schema.json").read_text())

        def prepare(config, output):
            write_json_atomic(output / "agent_source.json", {"files": ["run_agent.py"]})
            return source

        config = {
            "scope": "test",
            "dataset_discovery": {"enabled": False},
            "source": {},
            "orchestrator": {},
            "target": {},
            "adapter": {},
            "artifacts_root": str(self.root / "runs"),
            "limits": {"rounds": 1, "generations": 1, "initial_tests": 1, "active_tests": 2},
            "seed_scenarios": {
                "directory": str(seeds),
                "sha256": hashes,
                "evidence": {str(prior): sha256_file(prior)},
            },
        }
        with patch("src.orchestrator.runner.prepare_source", side_effect=prepare):
            output = run(config, Planner, lambda _: Adapter())
        self.assertEqual(2, observed[0]["max_active_tests"])
        self.assertEqual(2, observed[0]["seed_suite"]["count"])
        self.assertEqual(1, len(observed[0]["seed_suite"]["prior_evidence"]))
        self.assertEqual(2, len(list((output / "working/scenarios").glob("*.json"))))


if __name__ == "__main__":
    unittest.main()
