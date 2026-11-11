"""Campaign CLI contracts without an installed agent or provider request."""

import json
import shutil
from pathlib import Path
from unittest.mock import patch

from src.evaluation import evaluate
from tests.helpers import TemporaryDirectoryTestCase, scenario


class CampaignTests(TemporaryDirectoryTestCase):
    def test_failed_task_does_not_stop_campaign_and_source_is_frozen(self):
        project = self.root / "project"
        (project / "scenarios").mkdir(parents=True)
        (project / "src/evaluation").mkdir(parents=True)
        (project / "src/__init__.py").write_text("")
        shutil.copy2(evaluate.ROOT / "scenario.schema.json", project / "scenario.schema.json")
        (project / "src/evaluation/eval_worker.py").write_text("# frozen worker\n")
        for name in ("first", "second"):
            (project / "scenarios" / f"{name}.json").write_text(json.dumps(scenario(name)))
        outcomes = []

        def execute(spec, config, key, **kwargs):
            outcomes.append(spec["id"])
            self.assertTrue(Path(config["worker_path"]).is_file())
            return {"id": spec["id"], "status": "failed" if len(outcomes) == 1 else "passed"}

        output = self.root / "run"
        with (
            patch.object(evaluate, "ROOT", project),
            patch.object(evaluate, "run_case", side_effect=execute),
            patch("src.evaluation.analyze.analyze"),
        ):
            self.assertEqual(0, evaluate.main(["--output", str(output)]))
        self.assertEqual(["first", "second"], outcomes)
        report = json.loads((output / "report.json").read_text())
        self.assertEqual("complete", report["status"])
        self.assertEqual(["failed", "passed"], [c["status"] for c in report["cases"]])
        self.assertTrue((output / "source/scenario.schema.json").is_file())
        (project / "src/evaluation/eval_worker.py").write_text("# later edit\n")
        self.assertEqual(
            "# frozen worker\n", (output / "source/src/evaluation/eval_worker.py").read_text()
        )

    def test_invalid_task_fails_before_creating_output(self):
        source = self.root / "broken.json"
        source.write_text("{broken")
        output = self.root / "run"
        with self.assertRaises(SystemExit) as stopped:
            evaluate.main(["--scenarios", str(source), "--output", str(output)])
        self.assertEqual(2, stopped.exception.code)
        self.assertFalse(output.exists())
