"""Arbitrary source ingestion and custom runtime contracts, without target inference."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import httpx

from src.evaluation.runtime_support import write_json_atomic
from src.orchestrator import app
from src.orchestrator.adapters import make_adapter
from src.orchestrator.dashboard import Catalog, identity
from src.orchestrator.datasets import discover
from src.orchestrator.runner import prepare_source, run
from src.orchestrator.source import read_source, snapshot_directory, verify_snapshot
from src.orchestrator.workspace import Workspace
from tests.helpers import TemporaryDirectoryTestCase
from tests.test_dataset_discovery import catalogue_response
from tests.test_orchestrator import ROOT, case


class CustomAgentTests(TemporaryDirectoryTestCase):
    def test_native_source_only_design_does_not_inspect_docker(self):
        for kind in ("openhands", "deerflow"):
            with (
                self.subTest(kind=kind),
                patch("src.orchestrator.adapters.subprocess.check_output") as docker,
            ):
                adapter = make_adapter(
                    {"kind": kind, "design_only": True, "source_revision": "abc"}
                )
                self.assertFalse(adapter.describe()["execution_available"])
                self.assertEqual(kind, adapter.describe()["framework"])
                docker.assert_not_called()

    def source(self):
        source = self.root / "agent"
        source.mkdir()
        (source / "agent.ts").write_text("export const tools = ['read_repository', 'edit_code'];\n")
        (source / "README.md").write_text("A coding assistant that fixes repository problems.")
        return source

    def test_directory_snapshot_includes_working_code_and_excludes_secrets_and_links(self):
        source = self.source()
        (source / ".env").write_text("PRIVATE=secret")
        (source / "node_modules").mkdir()
        (source / "node_modules/dependency.js").write_text("external dependency")
        outside = self.root / "outside.py"
        outside.write_text("private outside source")
        (source / "linked.py").symlink_to(outside)
        config = snapshot_directory(source, self.root / "snapshot")
        frozen = verify_snapshot(config)
        self.assertEqual({"agent.ts", "README.md"}, set(config["sha256"]))
        self.assertEqual(
            (source / "agent.ts").read_text(), read_source(frozen, "agent.ts").read_text()
        )
        for name in ("../outside.py", ".env", "linked.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                read_source(frozen, name)
        (source / "agent.ts").write_text("modified after snapshot")
        self.assertEqual(frozen, verify_snapshot(config))
        (frozen / "agent.ts").write_text("tampered snapshot")
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            verify_snapshot(config)

    def test_custom_local_git_snapshot_includes_uncommitted_and_untracked_files(self):
        source = self.source()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "add", "agent.ts"], check=True)
        (source / "agent.ts").write_text("uncommitted source")
        config = app.resolve_source(
            {"source_input": str(source), "adapter": {"kind": "custom"}, "execute": False},
            self.root / "launch",
        )
        self.assertEqual("directory", config["source"]["kind"])
        self.assertIn("README.md", config["source"]["sha256"])
        self.assertEqual(
            "uncommitted source", (Path(config["source"]["local_path"]) / "agent.ts").read_text()
        )

    def test_repository_without_revision_is_resolved_and_pinned(self):
        source = self.source()

        def git(*args):
            return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()

        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial")
        config = {"adapter": {"kind": "custom"}, "source": {"repository": source.as_uri()}}
        output = self.root / "run"
        output.mkdir()
        frozen = prepare_source(config, output)
        self.assertEqual(git("rev-parse", "HEAD"), config["source"]["revision"])
        self.assertTrue((frozen / "agent.ts").is_file())

    def test_dashboard_custom_design_needs_no_image_factory_or_target_secret(self):
        source = self.source()
        request = {
            "runtime": "custom",
            "source": str(source),
            "execute": False,
            "python": sys.executable,
            "orchestrator": {
                "base_url": "http://localhost:1234/v1",
                "model": "planner",
                "api_key_env": "PLAN_KEY",
            },
            "target": {
                "base_url": "http://localhost:1234/v1",
                "model": "target",
                "api_key_env": "TARGET_KEY",
            },
        }
        with patch.object(
            app, "load_eval_config", return_value={"PLAN_KEY": "private", "TARGET_KEY": "target"}
        ):
            config = app.validate_request(request, Catalog(self.root))
            self.assertNotIn("runtime_image", config["adapter"])
            self.assertTrue(config["dataset_discovery"]["enabled"])
            with self.assertRaisesRegex(ValueError, "adapter factory"):
                app.validate_request({**request, "execute": True}, Catalog(self.root))
        with patch.object(app, "load_eval_config", return_value={"PLAN_KEY": "private"}):
            app.validate_request(request, Catalog(self.root))

    def test_custom_source_discovery_precedes_generation_and_reaches_reviewers(self):
        source = self.source()
        events, tasks = [], []
        self.requests = []

        class Planner:
            def __init__(self, *_):
                pass

            def ask(self, messages):
                task = json.loads(messages[1]["content"][0]["text"])
                if len(messages) == 2:
                    tasks.append(task)
                name = task["task"]
                if name.startswith("Select relevant"):
                    events.append("selection")
                    return {
                        "selections": [
                            {
                                "id": "author/coding-sessions",
                                "reason": "Coding user requests match repository tools",
                            }
                        ]
                    }
                if name.startswith("Review"):
                    events.append("review")
                    return {"accepted": True, "issues": []}
                if len(messages) == 2:
                    return {"source": [{"path": "agent.ts"}]}
                ref = json.loads(messages[3]["content"])[0]["ref"]
                if name.startswith("Profile"):
                    events.append("profile")
                    return {
                        "purpose": "Repository coding assistant",
                        "queries": ["coding"],
                        "architecture_refs": [ref],
                    }
                if len(messages) == 4:
                    events.append("generation")
                    return {
                        "actions": [
                            {
                                "op": "write",
                                "path": "scenarios/task_a.json",
                                "content": json.dumps(case()),
                            }
                        ]
                    }
                return {
                    "ready": True,
                    "rationale": [
                        {
                            "id": "task_a",
                            "architecture_refs": [ref],
                            "workload_source": {
                                "dataset_id": "author/coding-sessions",
                                "basis": "sampled_user",
                                "sample_index": 0,
                                "adaptation": "A small deterministic coding fixture",
                            },
                        }
                    ],
                }

        def discover_offline(config, directory, **options):
            events.append("search")
            with httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: catalogue_response(request, self.requests)
                )
            ) as client:
                result = discover(config, directory, client=client, **options)
            events.append("sources_ready")
            return result

        config = {
            "scope": "coding workflows",
            "source": {"local_path": str(source)},
            "adapter": {"kind": "custom"},
            "orchestrator": {},
            "target": {},
            "limits": {"rounds": 1, "generations": 2},
            "artifacts_root": str(self.root / "runs"),
        }
        output = run(config, Planner, dataset_discoverer=discover_offline)
        self.assertLess(events.index("sources_ready"), events.index("generation"))
        self.assertEqual(3, events.count("review"))
        for task in tasks:
            if task["task"].startswith(("Generate", "Review")):
                self.assertEqual(
                    "author/coding-sessions", task["dataset_sources"]["sources"][0]["id"]
                )
        state = json.loads((output / "campaign.json").read_text())
        self.assertEqual("generated_only", state["stop_reason"])
        detail = Catalog(self.root / "runs").detail(identity(output))
        self.assertEqual("source_guided_search", detail["datasets"]["selection_method"])
        self.assertIn(
            identity(output), {card["id"] for card in Catalog(self.root / "runs").cards()}
        )
        self.assertTrue((source / "agent.ts").is_file())

    def plugin(self, *, mutate=False, cases=None):
        path = self.root / "integration.py"
        path.write_text(
            """from src.evaluation.runtime_support import write_json_atomic
class Integration:
    def describe(self):
        return {"framework": "Example coding agent", "reference_tools": ["read_repository"]}
    def preflight(self, evaluator, output):
        MUTATION
        return {"passed": True, "reference_validation": "fixture"}
    def execute_revision(self, evaluator, output, target, key):
        write_json_atomic(output / "report.json", {"status": "complete", "cases": CASES})
        return {"status": "complete"}
def create_adapter(config):
    return Integration()
""".replace(
                "MUTATION",
                '(evaluator / "scenarios/task_a.json").write_text("{}")' if mutate else "pass",
            ).replace("CASES", repr(cases or []))
        )
        return make_adapter({"kind": "custom", "factory": str(path) + ":create_adapter"})

    def evaluator(self):
        workspace = Workspace(self.root / "evaluator", ROOT)
        write_json_atomic(workspace.root / "scenarios/task_a.json", case())
        return workspace.root

    def test_custom_adapter_requires_integration_for_execution_and_checks_tool_catalog(self):
        evaluator = self.evaluator()
        adapter = make_adapter({"kind": "custom"})
        self.assertFalse(adapter.describe()["execution_available"])
        self.assertFalse(adapter.preflight(evaluator, self.root / "preflight")["passed"])
        spec = case()
        spec["turns"][0]["reference"] = {
            "actions": [{"tool": "unknown_native_tool", "arguments": {}}],
            "final_response": "4",
        }
        write_json_atomic(evaluator / "scenarios/task_a.json", spec)
        result = self.plugin().preflight(evaluator, self.root / "plugin_preflight")
        self.assertFalse(result["passed"])
        spec["turns"][0]["reference"]["actions"][0]["tool"] = "read_repository"
        write_json_atomic(evaluator / "scenarios/task_a.json", spec)
        self.assertTrue(self.plugin().preflight(evaluator, self.root / "valid_preflight")["passed"])

    def test_custom_adapter_rejects_mutation_missing_cases_and_absent_phase_evidence(self):
        evaluator = self.evaluator()
        result = self.plugin(mutate=True).preflight(evaluator, self.root / "mutated")
        self.assertFalse(result["passed"])
        self.assertIn("modified", result["error"])
        write_json_atomic(evaluator / "scenarios/task_a.json", case())
        for cases in ([], [{"id": "task_a", "branches": {"ordinary": {"status": "passed"}}}]):
            with self.subTest(cases=cases):
                result = self.plugin(cases=cases).execute_revision(
                    evaluator, self.root / "execution", {}, "unused"
                )
                self.assertEqual("invalid_evaluator", result["status"])
