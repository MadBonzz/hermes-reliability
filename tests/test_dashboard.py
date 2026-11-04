"""Dashboard lifecycle, source selection and evidence presentation without model calls."""

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import httpx

from src.evaluation.runtime_support import sha256_file, write_json_atomic
from src.orchestrator import app
from src.orchestrator.dashboard import Catalog, identity
from src.orchestrator.findings import FindingsWorker, validate_presentation
from src.orchestrator.runner import run
from src.orchestrator.workspace import Workspace
from tests.helpers import TemporaryDirectoryTestCase
from tests.test_orchestrator import ROOT, case


class DashboardTests(TemporaryDirectoryTestCase):
    def test_fresh_checkout_loads_three_read_only_examples_without_api_calls(self):
        catalog = Catalog(self.root / "empty", ROOT / "examples/campaigns")
        cards = catalog.cards()
        self.assertEqual({"hermes", "openhands", "deerflow"}, {r["runtime"] for r in cards})
        self.assertEqual(3, len(cards))
        for card in cards:
            self.assertTrue(card["bundled"])
            detail = catalog.detail(card["id"])
            self.assertEqual(3, len(detail["rounds"]))
            for round in detail["rounds"]:
                findings = round["findings"]
                self.assertEqual("ready", findings["status"])
                validate_presentation(findings["presentation"], findings["sources"])
            with self.assertRaisesRegex(ValueError, "read-only"):
                catalog.resolve(card["id"])
        worker = FindingsWorker(catalog, self.root / ".env")
        with (
            patch("src.orchestrator.findings.generate") as generate,
            patch.object(worker.stop, "wait", side_effect=lambda _: worker.stop.set()),
        ):
            worker.run()
        generate.assert_not_called()

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

    def request(self):
        return {
            "runtime": "hermes",
            "source": "https://example.com/agent.git",
            "python": sys.executable,
            "orchestrator": {
                "model": "muse-wonder-1.0",
                "base_url": app.META_URL,
                "api_key_env": "META_KEY",
            },
            "target": {
                "model": "muse-spark-1.3-contributor",
                "base_url": app.META_URL,
                "api_key_env": "META_KEY",
            },
        }

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

    def test_sidebar_keeps_active_and_completed_runs_with_final_findings(self):
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
            self.assertEqual([], catalog.cards())
        write_json_atomic(path / "campaign.json", {"stop_reason": "execution_round_limit"})
        self.assertEqual(1, len(catalog.cards()))
        (path / "round_002").mkdir()
        self.assertEqual([], catalog.cards())
        self.assertTrue((path / "round_001/analysis.json").exists())

    def test_request_validates_credentials_budgets_and_native_settings(self):
        with patch.object(app, "load_eval_config", return_value={"META_KEY": "private"}):
            value = app.validate_request(self.request(), Catalog(self.root))
            self.assertNotIn("private", json.dumps(value))
            self.assertEqual(3, value["limits"]["rounds"])
            self.assertNotIn("max_output_tokens", value["orchestrator"])
            for change in (
                {"limits": {"rounds": 99}},
                {"source": "-option"},
                {"runtime": "unknown"},
                {"runtime": "deerflow"},
            ):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    app.validate_request({**self.request(), **change}, Catalog(self.root))
        with patch.object(app, "load_eval_config", return_value={}):
            with self.assertRaisesRegex(ValueError, "Set META_KEY"):
                app.validate_request(self.request(), Catalog(self.root))

    def test_launch_freezes_controller_and_uses_private_environment(self):
        with patch.object(app, "load_eval_config", return_value={"META_KEY": "secret"}):
            config = app.validate_request(self.request(), Catalog(self.root))
            with patch.object(app.subprocess, "Popen") as popen:
                popen.return_value.pid = 123
                result = app.launch(config, self.root)
            folder = self.root / "dashboard/launches" / result["id"]
            self.assertTrue((folder / "controller/src/orchestrator/web/index.html").is_file())
            self.assertTrue((folder / "controller/scenario.schema.json").is_file())
            self.assertFalse((folder / "controller/.env").exists())
            self.assertNotIn("secret", (folder / "config.json").read_text())
            self.assertEqual("secret", popen.call_args.kwargs["env"]["META_KEY"])

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

    def test_worker_records_failure_and_redacts_credentials(self):
        folder = self.root / "launch"
        with patch.object(app, "load_eval_config", return_value={"META_KEY": "secret"}):
            config = app.validate_request(self.request(), Catalog(self.root))
        write_json_atomic(folder / "config.json", config)
        with (
            patch.dict(os.environ, {"META_KEY": "secret"}),
            patch.object(app, "resolve_source", side_effect=ValueError("bad secret")),
        ):
            self.assertEqual(2, app.worker(folder))
        record = (folder / "launch.json").read_text()
        self.assertNotIn("secret", record)
        self.assertEqual("error", json.loads(record)["status"])

    def test_http_requires_session_token_and_rejects_foreign_hosts(self):
        path = self.campaign()
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.handler(Catalog(self.root), "token"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            with httpx.Client(base_url=url) as client:
                page = client.get("/")
                self.assertIn('content="token"', page.text)
                self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
                self.assertEqual(
                    403, client.get("/api/campaigns", headers={"Host": "evil.example"}).status_code
                )
                self.assertEqual(
                    403, client.post("/api/stop", json={"id": identity(path)}).status_code
                )
                response = client.post(
                    "/api/stop", json={"id": identity(path)}, headers={"X-Session-Token": "token"}
                )
                self.assertEqual(200, response.status_code)
                self.assertTrue((path / "stop.json").is_file())
                self.assertEqual(404, client.get("/.env").status_code)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
