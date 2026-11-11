"""Credential, source-read and persistent-approval boundaries from catalog review."""

import json
import os
import sys
from unittest.mock import Mock, patch

from src.orchestrator.adapters import ContainerAdapter, HermesAdapter
from src.orchestrator.findings import FindingsWorker
from src.orchestrator.source import read_source
from src.plugin.integration import register
from src.plugin.service import Service
from src.plugin.source import resolve_source
from tests.helpers import TemporaryDirectoryTestCase
from tests.host_safety import host_safety

UNRELATED = {
    "OPENAI_API_KEY": "wrong-profile-key",
    "DATABASE_PASSWORD": "test-password",
    "AWS_SECRET_ACCESS_KEY": "test-secret",
    "DATABASE_URL": "postgresql://user:password@private/db",
    "COOKIE": "test-cookie",
    "RELIABILITY_TARGET_API_KEY": "wrong-profile-target",
    "HERMES_YOLO": "1",
    "HERMES_HOME": "/private/host-profile",
    "PYTHONPATH": "/private/import-hooks",
}


def child_command(path):
    script = (
        "import os,json,pathlib; "
        f"p=pathlib.Path({str(path)!r}); p.parent.mkdir(parents=True,exist_ok=True); "
        "p.write_text(json.dumps({'passed':True,'status':'complete','cases':[],"
        "'environment':dict(os.environ)}))"
    )
    return [sys.executable, "-c", script]


class ChildEnvironmentTests(TemporaryDirectoryTestCase):
    def test_preflight_and_execution_children_receive_only_explicit_phase_credentials(self):
        evaluator = self.root / "evaluator"
        evaluator.mkdir()
        for kind in ("hermes", "openhands", "deerflow"):
            with self.subTest(kind=kind):
                target = {"model": "fixture", "base_url": "http://localhost/v1",
                          "api_key_env": "RELIABILITY_TARGET_API_KEY"}
                config = {"kind": kind, "design_only": True, "target": target}
                adapter = HermesAdapter(config) if kind == "hermes" else ContainerAdapter(config)
                adapter.target_api_key = "owning-profile-target"
                adapter.validation_command = lambda ev, out: child_command(out / "report.json")
                adapter.execution_command = lambda ev, out, target: child_command(out / "report.json")
                folder = self.root / kind
                with patch.dict(os.environ, {**UNRELATED, "PATH": os.defpath, "LANG": "C.UTF-8"}, clear=True):
                    preflight = adapter.preflight(evaluator, folder / "review-preflight")
                    result = adapter.execute_revision(evaluator, folder / "execution", target, "owning-profile-target")
                self.assertTrue(preflight["passed"])
                self.assertEqual("complete", result["status"])
                execution = json.loads((folder / "execution/report.json").read_text())
                for record in (preflight, execution):
                    self.assertFalse(set(UNRELATED) & set(record["environment"]))
                self.assertNotIn("EVAL_API_KEY", preflight["environment"])
                self.assertEqual("owning-profile-target", execution["environment"]["EVAL_API_KEY"])
                if kind == "hermes":
                    self.assertNotIn("TASK_API_KEY", preflight["environment"])
                else:
                    self.assertEqual("owning-profile-target", preflight["environment"]["TASK_API_KEY"])

    def test_findings_thread_uses_captured_profile_values_not_global_environment(self):
        analysis = self.root / "campaign/round_001/analysis.json"
        analysis.parent.mkdir(parents=True)
        analysis.write_text("{}")
        catalog = Mock()
        catalog.cards.return_value = [{"id": "test"}]
        catalog.resolve.return_value = analysis.parents[1]
        values = {"RELIABILITY_TARGET_API_KEY": "owning-profile-target"}
        client = Mock()
        worker = FindingsWorker(catalog, credential_values=values, client_factory=client)
        values.clear()

        def generate(path, scoped, factory):
            self.assertEqual(analysis.parent, path)
            self.assertEqual({"RELIABILITY_TARGET_API_KEY": "owning-profile-target"}, scoped)
            self.assertIs(client, factory)
            worker.stop.set()

        with patch.dict(os.environ, UNRELATED), patch("src.orchestrator.findings.generate", side_effect=generate):
            worker.run()


class SourceAndApprovalTests(TemporaryDirectoryTestCase):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.profile = self.root / "profile"
        self.source = self.home / "project"
        self.source.mkdir(parents=True)
        self.profile.mkdir()
        (self.source / "agent.py").write_text("def answer(): return 4\n")
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))
        self.enterContext(host_safety(self.home, self.profile, blocked=[self.root / "host-denied"]))

    def test_design_only_rejects_home_profiles_guard_denials_and_symlink_aliases(self):
        service = Service(self.root / "jobs", options={})
        (self.profile / "nested").mkdir()
        (self.root / "host-denied").mkdir()
        alias = self.root / "alias"
        alias.symlink_to(self.profile, target_is_directory=True)
        for forbidden in (self.home, self.profile, self.profile / "nested", self.root, alias,
                          self.root / "host-denied", self.home / "../profile"):
            with self.subTest(path=forbidden), self.assertRaises(ValueError):
                service.start({"source": str(forbidden), "execute": False}, spawn=False)
        self.assertFalse((service.root / "jobs").exists())
        self.assertEqual(str(self.source), service.validate({"source": str(self.source)})["source_input"])

    def test_snapshot_excludes_credential_stores_and_rechecks_worker_policy(self):
        sensitive = ["auth.json", "config.yaml", ".netrc", ".git-credentials", ".config/provider/token",
                     ".gnupg/keyring", ".docker/config.json", ".kube/config", "browser-profile/Cookies",
                     ".mozilla/firefox/profile/logins.json", "User Data/Default/Login Data",
                     "Library/Application Support/Google/Chrome/Default/Login Data"]
        for name in sensitive:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("private fixture")
        denied = self.source / "other.py"
        denied.write_text("blocked by host")
        config = {"source_input": str(self.source), "adapter": {"kind": "custom"}, "execute": False}
        with host_safety(self.home, self.profile, blocked=[denied]):
            resolve_source(dict(config), self.root / "snapshot")
        copied = self.root / "snapshot/agent_source"
        self.assertEqual(["agent.py"], [str(p.relative_to(copied)) for p in copied.rglob("*") if p.is_file()])
        for name in sensitive:
            with self.subTest(name=name), self.assertRaises(ValueError):
                read_source(self.source, name)
        with host_safety(self.home, self.profile, blocked=[self.source]):
            with self.assertRaises(ValueError):
                resolve_source(dict(config), self.root / "blocked-snapshot")
        self.assertFalse((self.root / "blocked-snapshot").exists())

    def test_always_approval_is_scoped_to_source_revision_runtime_and_mode(self):
        ctx = Mock()
        register(ctx)
        hook = ctx.register_hook.call_args.args[1]
        original = {"source": str(self.source), "execute": True, "runtime": "custom"}
        other_local = self.source.with_suffix(".git")
        other_local.mkdir()
        first = hook("reliability_start", original)
        self.assertEqual("approve", first["action"])
        self.assertEqual(first["rule_key"], hook("reliability_start", dict(original))["rule_key"])
        for change in ({"source": "https://example.com/other.git"}, {"source": str(other_local)}, {"revision": "new-commit"},
                       {"runtime": "hermes"}, {"mode": "regression"}):
            with self.subTest(change=change):
                self.assertNotEqual(first["rule_key"], hook("reliability_start", {**original, **change})["rule_key"])
        self.assertIsNone(hook("reliability_start", {**original, "execute": False}))
