import os
from types import SimpleNamespace
from unittest.mock import patch

from src import eval_runner as runner
from src import utils
from tests.helpers import FakeAgent, RegistryTestCase


class UtilsTests(RegistryTestCase):
    def test_path_escape_and_absolute_outside_rejected(self):
        for path in ("../outside", str(self.root.parent / "outside")):
            with self.assertRaises(ValueError):
                runner.resolve_ws_path(self.root, path)
        self.assertEqual(
            self.root / "nested/file", runner.resolve_ws_path(self.root, "{WORKSPACE}/nested/file")
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
            patch.object(runner.sys, "path", list(runner.sys.path)),
            patch.dict(os.environ, {}, clear=False),
            patch.object(utils.importlib, "import_module", side_effect=lambda name: modules[name]),
        ):
            selected = runner.load_hermes(str(root))
            self.assertEqual(root, selected)
            self.assertEqual(str(root), runner.sys.path[0])

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
