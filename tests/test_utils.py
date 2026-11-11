import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from src.evaluation import evaluate, utils
from src.evaluation.evaluate import parse_args
from tests.helpers import FakeAgent, RegistryTestCase


class UtilsTests(RegistryTestCase):
    def test_unauthenticated_target_does_not_fall_back_to_host_credentials(self):
        class Agent:
            def __init__(self, api_key=None, provider=None, base_url=None):
                self.api_key, self.provider, self.base_url = api_key, provider, base_url

        with patch.object(utils, "AIAgent", Agent):
            anonymous = utils.instantiate_hermes_agent("http://localhost:1234/v1", "test", "", 3)
            self.assertEqual(
                ("unused", "custom", "http://localhost:1234/v1"),
                (anonymous.api_key, anonymous.provider, anonymous.base_url),
            )
            explicit = utils.instantiate_hermes_agent(
                "https://model.test/v1", "test", "target-key", 3
            )
            self.assertEqual("target-key", explicit.api_key)

    def test_bridge_extraction_preserves_order_arguments_and_rejects_malformed(self):
        entries = [
            {"name": "get_event", "arguments": {}},
            {"name": "update_event", "arguments": {"expected_version": 2}},
        ]
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "tool_call", "arguments": json.dumps({"calls": [e]})}}
                    for e in entries
                ],
            }
        ]
        self.assertEqual(entries, utils.extract_turn_tool_calls(messages, 0))
        for invalid in (
            "{",
            {"calls": []},
            {"calls": entries},
            {"name": "get_event", "arguments": []},
            {"name": "tool_call", "arguments": {}},
        ):
            self.assertEqual(("tool_call", invalid), utils.unwrap_tool_call("tool_call", invalid))
        self.assertEqual(
            ("get_event", {}), utils.unwrap_tool_call("tool_call", {"name": "get_event"})
        )

    def test_baseten_profile_key_aliases_and_explicit_overrides(self):
        for name in ("BASETEN_API_KEY", "BASETEN_KEY", "BAESTEN_KEY"):
            with patch.object(evaluate, "load_eval_config", return_value={name: "test-key"}):
                _, args = parse_args(["--provider", "baseten"])
                self.assertEqual("test-key", args.api_key)
                self.assertEqual("https://inference.baseten.co/v1", args.base_url)
                self.assertEqual("meta-models/muse-spark-open-1026", args.model)
                self.assertEqual(0, args.request_interval)
        with patch.object(evaluate, "load_eval_config", return_value={}):
            _, args = parse_args(
                [
                    "--provider",
                    "baseten",
                    "--api-key=explicit",
                    "--model=exact",
                    "--base-url=https://example.test/v1",
                ]
            )
            self.assertEqual(
                ("explicit", "exact", "https://example.test/v1"),
                (args.api_key, args.model, args.base_url),
            )

    def test_cohere_profile_cli_overrides_and_required_reasoning(self):
        with patch.object(evaluate, "load_eval_config", return_value={"COHERE_KEY": "test-key"}):
            _, args = parse_args(["--provider", "cohere"])
            self.assertEqual("command-a-plus-05-2026", args.model)
            self.assertEqual("test-key", args.api_key)
            _, overridden = parse_args(
                [
                    "--provider",
                    "cohere",
                    "--model=explicit-model",
                    "--base-url",
                    "http://local/v1",
                    "--api-key",
                    "explicit-key",
                ]
            )
            self.assertEqual(
                ("explicit-model", "http://local/v1", "explicit-key"),
                (overridden.model, overridden.base_url, overridden.api_key),
            )

        class Agent:
            def __init__(self, reasoning_config=None):
                self.reasoning_config = reasoning_config

        with patch.object(utils, "AIAgent", Agent):
            agent = utils.instantiate_hermes_agent(args.base_url, args.model, args.api_key, 3)
            self.assertEqual({"enabled": True, "effort": "high"}, agent.reasoning_config)

    def test_run_environment_is_fresh_pins_context_and_restores_on_error(self):
        home = self.root / "runtime_home"
        with patch.dict(os.environ, {"HERMES_HOME": "operator-profile"}):
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                with utils.hermes_run_environment(home):
                    self.assertEqual(str(home.resolve()), os.environ["HERMES_HOME"])
                    self.assertEqual(
                        utils.EVAL_HERMES_CONFIG, json.loads((home / "config.yaml").read_text())
                    )
                    raise RuntimeError("test failure")
            self.assertEqual("operator-profile", os.environ["HERMES_HOME"])
            with self.assertRaises(FileExistsError):
                with utils.hermes_run_environment(home):
                    self.fail("Existing profile must not be reused")
            self.assertEqual("operator-profile", os.environ["HERMES_HOME"])

    def test_run_environment_removes_home_override_when_previously_absent(self):
        with patch.dict(os.environ, {}, clear=True):
            with utils.hermes_run_environment(self.root / "new_home"):
                self.assertIn("HERMES_HOME", os.environ)
            self.assertNotIn("HERMES_HOME", os.environ)

    def test_path_escape_and_absolute_outside_rejected(self):
        for path in ("../outside", str(self.root.parent / "outside")):
            with self.assertRaises(ValueError):
                utils.resolve_ws_path(self.root, path)
        self.assertEqual(
            self.root / "nested/file", utils.resolve_ws_path(self.root, "{WORKSPACE}/nested/file")
        )

    def test_explicit_hermes_root_is_resolved_before_imports(self):
        root = self.root / "checkout"
        root.mkdir()
        (root / "run_agent.py").write_text("# test checkout")
        modules = {
            "model_tools": SimpleNamespace(),
            "toolsets": self.toolsets,
            "tools.registry": SimpleNamespace(registry=self.registry),
            "run_agent": SimpleNamespace(AIAgent=FakeAgent),
        }
        with (
            patch.object(utils, "REPO_ROOT", None),
            patch.object(utils, "AIAgent", None),
            patch.object(utils.sys, "path", list(utils.sys.path)),
            patch.dict(os.environ, {}, clear=False),
            patch.object(utils.importlib, "import_module", side_effect=lambda name: modules[name]),
        ):
            selected = utils.load_hermes(str(root))
            self.assertEqual(root, selected)
            self.assertEqual(str(root), utils.sys.path[0])

    def test_servers_use_chat_completions_with_exact_connection_settings(self):
        class CapturingAgent:
            def __init__(self, model, provider, base_url, api_key, api_mode, max_iterations, cwd):
                self.connection = (model, provider, base_url, api_key, api_mode)
                self.cwd = cwd
                self._persist_disabled = False

        with patch.object(utils, "AIAgent", CapturingAgent):
            for url, model, key in [
                (
                    "https://openrouter.ai/api/v1",
                    "meta-llama/llama-3.3-70b-instruct",
                    "provider-key",
                ),
                ("http://localhost:11434/v1", "qwen3:8b", "ollama"),
                ("http://localhost:1235/v1", "nvidia/nemotron-3-nano-4b", "lm-studio"),
            ]:
                with self.subTest(url=url):
                    agent = utils.instantiate_hermes_agent(url, model, key, 3, self.root)
                    self.assertEqual(
                        (model, "custom", url, key, "chat_completions"), agent.connection
                    )
                    self.assertEqual(str(self.root), agent.cwd)
                    self.assertTrue(agent._persist_disabled)
