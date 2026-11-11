"""Plugin registration, credential boundary, job lifecycle and real campaign integration."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from src.evaluation.api_trace import SecretRedactor
from src.evaluation.runtime_support import write_json_atomic
from src.evaluation.utils import load_eval_config
from src.orchestrator.dashboard import identity
from src.orchestrator.runner import CampaignPreempted, exchange
from src.plugin.client import HermesClient
from src.plugin.integration import register
from src.plugin.service import Service
from src.plugin.snapshot import verify
from tests.helpers import TemporaryDirectoryTestCase
from tests.host_safety import HostSafetyMixin
from tests.test_orchestrator import ROOT, case


class FixtureLlm:
    """Deterministic model fixture, never presented as a target evaluation."""

    def __init__(self):
        self.tasks = []

    def complete(self, *, messages, **kwargs):
        task = json.loads(messages[1]["content"][0]["text"])
        name = task["task"]
        self.tasks.append(name)
        if name.startswith("Review"):
            answer = {
                "accepted": True,
                "issues": [],
                "assessments": [
                    {"id": "task_a", "verdict": "accept", "reason": "Fixture contract is explicit"}
                ],
            }
        elif len(messages) == 2:
            answer = {"source": [{"path": "agent.py"}]}
        elif len(messages) == 4:
            answer = {
                "actions": [
                    {"op": "write", "path": "scenarios/task_a.json", "content": json.dumps(case())}
                ]
            }
        else:
            ref = json.loads(messages[3]["content"])[0]["ref"]
            answer = {
                "ready": True,
                "rationale": [{"id": "task_a", "architecture_refs": [ref]}],
                "design_evidence": [
                    {
                        "id": "task_a",
                        "invariant": "Answer contains the calculated number",
                        "exposure": "User asks a deterministic question",
                        "architecture_route": "answer()",
                        "complexity_reason": "Single-turn fixture",
                        "control_reason": "No fault in this test fixture",
                        "contract_checks": [
                            {
                                "check_ids": ["ordinary:1:output:0"],
                                "visible_requirement": "Calculate two plus two",
                                "valid_alternative": "The answer is 4",
                                "invalid_example": "The answer is 5",
                            }
                        ],
                    }
                ],
            }
        return SimpleNamespace(
            text=json.dumps(answer),
            usage={"input_tokens": 10, "output_tokens": 5},
            provider="offline-fixture",
            model="fixture",
            audit={"plugin_id": "hermes-reliability"},
        )


class PluginTests(HostSafetyMixin, TemporaryDirectoryTestCase):
    def source(self):
        source = self.root / "agent"
        source.mkdir()
        (source / "agent.py").write_text('def answer(): return "4"\n')
        return source

    def service(self, **options):
        return Service(
            self.root / "profile", options={"dataset_discovery": {"enabled": False}, **options}
        )

    def ctx(self, llm=None, execution=False):
        return SimpleNamespace(
            llm=llm or FixtureLlm(),
            get_config=lambda key, default=None: (
                execution if key == "execution_enabled" else default
            ),
        )

    def secret_scope(self):
        scope = ModuleType("agent.secret_scope")
        scope.get_secret = lambda name: None
        return patch.dict(sys.modules, {"agent.secret_scope": scope})

    def test_registration_is_lazy_and_manifest_matches_native_tools_and_hook(self):
        ctx = Mock()
        with patch(
            "src.plugin.service.Service",
            side_effect=AssertionError("registration must not create state"),
        ):
            register(ctx)
        manifest = json.loads((ROOT / "plugin.yaml").read_text())
        self.assertEqual(
            set(manifest["provides_tools"]),
            {c.kwargs["name"] for c in ctx.register_tool.call_args_list},
        )
        hook = ctx.register_hook.call_args.args[1]
        self.assertIsNone(hook("reliability_start", {"source": "/example"}))
        self.assertEqual("approve", hook("reliability_start", {"source": "https://example.com/a", "execute": True})["action"])
        self.assertEqual("reliability", ctx.register_cli_command.call_args.args[0])

    def test_synthetic_plugin_namespace_does_not_replace_global_src(self):
        name = "test_reliability_plugin_isolated"
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
        )
        module = importlib.util.module_from_spec(spec)
        existing = sys.modules.get("src")
        with patch.dict(sys.modules, {name: module}):
            spec.loader.exec_module(module)
            module.register(Mock())
            self.assertIs(existing, sys.modules.get("src"))

    def test_default_design_needs_no_runtime_or_target_keys_and_rejects_code_parameters(self):
        request = {"source": str(self.source())}
        service = self.service()
        config = service.validate(request)
        self.assertFalse(config["execute"])
        self.assertEqual("custom", config["adapter"]["kind"])
        self.assertNotIn("factory", config["adapter"])
        for extra in (
            {"execute": True},
            {"python": "/bad"},
            {"adapter_factory": "/bad.py:run"},
            {"execute": "false"},
            {"limits": {"requests": 100000}},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                service.validate({**request, **extra})

    def test_no_repository_credential_files_are_loaded(self):
        env_file = self.root / ".env"
        env_file.write_text("EVAL_API_KEY=must-not-load\n")
        with patch.dict("os.environ", {"EVAL_MODEL": "explicit"}, clear=True):
            self.assertEqual({"EVAL_MODEL": "explicit"}, load_eval_config())

    def test_invalid_repository_urls_and_budgets_fail_before_job_creation(self):
        service = self.service()
        for source in (
            "-bad",
            "https://user:secret@example.com/a",
            "https://example.com/a?token=secret",
            "file:///missing",
            "http://example.com/a",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                service.start({"source": source}, spawn=False)
        self.assertFalse((service.root / "jobs").exists())
        for limits in ({"rounds": True}, {"initial_tests": 5, "active_tests": 1}, {"rounds": 6}):
            with self.assertRaises(ValueError):
                service.start({"source": "https://example.com/repo", "limits": limits}, spawn=False)

    def test_execution_uses_only_operator_config_and_keeps_native_gate(self):
        service = self.service(
            execution_enabled=True,
            runtimes={"custom": {"factory": "/trusted/adapter.py:create_adapter"}},
            target={"model": "target", "base_url": "http://localhost:1234/v1"},
        )
        config = service.validate({"source": "https://example.com/repo", "execute": True})
        self.assertEqual("gate", config["adapter"]["hermes_approval"])
        self.assertEqual("RELIABILITY_TARGET_API_KEY", config["target"]["api_key_env"])

    def test_target_credentials_cannot_be_saved_as_configuration(self):
        for target in (
            {"base_url": "https://key:secret@example.com", "model": "target"},
            {"base_url": "https://example.com/v1", "model": "target", "api_key": "secret"},
        ):
            service = self.service(execution_enabled=True, target=target)
            with self.assertRaises(ValueError):
                service.start(
                    {"source": "https://example.com/repo", "runtime": "hermes", "execute": True},
                    spawn=False,
                )
            self.assertFalse((service.root / "jobs").exists())
        for value in (0, False, -1):
            with self.assertRaises(ValueError):
                self.service(requests=value).validate({"source": "https://example.com/repo"})

    def test_start_spawns_supported_cli_without_credentials_in_argv(self):
        service = self.service()
        with patch(
            "src.plugin.service.subprocess.Popen", return_value=SimpleNamespace(pid=123)
        ) as spawn:
            job = service.start({"source": str(self.source())})
        args = spawn.call_args.args[0]
        self.assertEqual(
            [sys.executable, "-m", "hermes_cli.main", "reliability", "worker", job["id"]], args
        )
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertEqual(
            str(service.root.parent.parent), spawn.call_args.kwargs["env"]["HERMES_HOME"]
        )
        self.assertTrue((service.job_path(job["id"]) / "controller_manifest.json").is_file())
        self.assertEqual("queued", job["status"])

    def test_real_design_campaign_uses_host_client_three_reviews_and_saved_evidence(self):
        service = self.service()
        source = self.source()
        job = service.start(
            {"source": str(source), "limits": {"rounds": 1, "generations": 2}}, spawn=False
        )
        llm = FixtureLlm()
        with (
            self.secret_scope(),
            patch(
                "src.evaluation.utils.load_eval_config",
                side_effect=AssertionError("host must own credentials"),
            ),
        ):
            result = service.worker(job["id"], self.ctx(llm))
        self.assertEqual("complete", result["status"], result)
        detail = service.catalog.detail(result["campaign_id"])
        self.assertEqual("generated_only", detail["stop_reason"])
        self.assertEqual(3, sum(name.startswith("Review") for name in llm.tasks))
        self.assertEqual(1, len(detail["tests"]))
        self.assertEqual({}, detail["rounds"][0]["lanes"])
        self.assertEqual("disabled", detail["datasets"]["status"])
        self.assertTrue((source / "agent.py").exists())
        output = Path(result["campaign_path"])
        self.assertTrue((output / "evidence_index.json").is_file())
        self.assertEqual(
            60,
            json.loads((output / "orchestrator_usage.json").read_text())["reported_tokens"][
                "input_tokens"
            ],
        )
        with self.assertRaises(FileExistsError):
            service.worker(job["id"], self.ctx())

    def test_controller_snapshot_is_independent_and_tampering_fails_before_inference(self):
        service = self.service()
        job = service.start({"source": str(self.source())}, spawn=False)
        folder = service.job_path(job["id"])
        controller = verify(folder)
        self.assertNotEqual(ROOT, controller)
        (controller / "src/orchestrator/prompts.py").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "Frozen controller changed"):
            service.worker(job["id"], self.ctx())

    def test_queued_stop_does_not_call_model_or_clone(self):
        service = self.service()
        job = service.start({"source": "https://example.com/repo"}, spawn=False)
        service.stop(job_id=job["id"])
        ctx = self.ctx(Mock())
        with patch("src.plugin.service.resolve_source") as prepare:
            result = service.worker(job["id"], ctx)
        self.assertEqual("stopped", result["status"])
        prepare.assert_not_called()
        ctx.llm.complete.assert_not_called()

    def test_revoked_execution_is_not_dispatched(self):
        service = self.service(
            execution_enabled=True, target={"base_url": "http://localhost:1234/v1", "model": "test"}
        )
        job = service.start(
            {"source": "https://example.com/repo", "runtime": "hermes", "execute": True},
            spawn=False,
        )
        with patch("src.plugin.service.resolve_source") as prepare:
            result = service.worker(job["id"], self.ctx(execution=False))
        self.assertEqual("error", result["status"])
        prepare.assert_not_called()

    def test_evidence_reader_rejects_traversal_and_links(self):
        service = self.service()
        root = service.catalog.root / "run/attempts/abc"
        write_json_atomic(root / "config.json", {})
        write_json_atomic(root / "evidence.json", {"ok": True})
        (root / "linked.json").symlink_to(root / "evidence.json")
        cid = identity(root)
        self.assertIn('"ok": true', service.evidence(cid, "evidence.json")["text"])
        for path in ("../../../private.json", "linked.json"):
            with self.assertRaises(ValueError):
                service.evidence(cid, path)

    def test_host_client_limits_cancellation_usage_and_provider_failure_redaction(self):
        cfg = {"max_request_characters": 1000, "max_output_tokens": 100, "timeout_seconds": 20}
        llm = Mock()
        llm.complete.return_value = SimpleNamespace(
            text='```json\n{"ready":true}\n```',
            usage={"input_tokens": 2, "output_tokens": 3},
            model="active",
            provider="host",
            audit={},
        )
        client = HermesClient(cfg, self.root, SecretRedactor(["secret"]), "", 1, llm=llm)
        self.assertEqual({"ready": True}, client.ask([{"role": "user", "content": "secret"}]))
        self.assertNotIn("secret", (self.root / "api_0001.request.json").read_text())
        self.assertNotIn("model", llm.complete.call_args.kwargs)
        with self.assertRaisesRegex(RuntimeError, "budget"):
            client.ask([])
        write_json_atomic(self.root / "stop.json", {"reason": "test"})
        with self.assertRaises(CampaignPreempted):
            client.ask([])

    def test_host_client_repairs_multiple_objects_through_controller_budget(self):
        cfg = {"max_request_characters": 30000, "max_output_tokens": 100, "timeout_seconds": 20}
        malformed = 'Scenario: {"id":"example"}\nActions: {"actions":[]}'
        llm = Mock()
        llm.complete.side_effect = [
            SimpleNamespace(text=text, usage={}, model="active", provider="host", audit={})
            for text in (malformed, '{"ready":true}')
        ]
        client = HermesClient(cfg, self.root, SecretRedactor([]), "", 2, llm=llm)
        conversation = []
        self.assertEqual(
            {"ready": True},
            exchange(client, None, {"task": "test"}, conversation=conversation),
        )
        self.assertIn("Multiple JSON objects", conversation[2]["content"])
        self.assertEqual(2, client.calls)
        saved = json.loads((self.root / "api_0001.response.json").read_text())
        self.assertEqual(malformed, json.loads(saved["body"])["choices"][0]["message"]["content"])
        with self.assertRaisesRegex(RuntimeError, "budget"):
            client.ask([])
