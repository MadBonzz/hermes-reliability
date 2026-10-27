import os
from pathlib import Path
from unittest.mock import patch

from src import eval_runner as runner
from tests.helpers import TemporaryDirectoryTestCase


class ConfigurationTests(TemporaryDirectoryTestCase):
    def test_dotenv_shell_and_cli_precedence_without_environment_mutation(self):
        (self.root / ".env").write_text(
            "EVAL_MODEL=file-model\nLM_STUDIO_URL=http://file/v1\n"
            'HERMES_AGENT_ROOT="/checkout with spaces"\nLM_STUDIO_API_KEY=literal-${TOKEN}\n',
            encoding="utf-8",
        )
        with (
            patch.dict(os.environ, {"EVAL_MODEL": "shell-model", "TOKEN": "expanded"}, clear=True),
            patch.object(runner, "PROJECT_ROOT", self.root),
        ):
            before = dict(os.environ)
            _, args = runner.parse_args([])
            self.assertEqual("shell-model", args.model)
            self.assertEqual("http://file/v1", args.base_url)
            self.assertEqual("/checkout with spaces", args.hermes_root)
            self.assertEqual("literal-${TOKEN}", args.api_key)
            _, args = runner.parse_args(
                [
                    "--model",
                    "cli-model",
                    "--base-url",
                    "http://cli/v1",
                    "--hermes-root",
                    "/cli",
                    "--api-key",
                    "cli-key",
                ]
            )
            self.assertEqual(
                ("cli-model", "http://cli/v1", "/cli", "cli-key"),
                (args.model, args.base_url, args.hermes_root, args.api_key),
            )
            self.assertEqual(before, dict(os.environ))

    def test_absent_dotenv_uses_defaults_without_loading_hermes(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(runner, "PROJECT_ROOT", self.root),
            patch.object(runner, "load_hermes") as load,
        ):
            _, args = runner.parse_args([])
            self.assertEqual("nvidia/nemotron-3-nano-4b", args.model)
            self.assertEqual("http://localhost:1235/v1", args.base_url)
            self.assertIsNone(args.hermes_root)
            self.assertFalse(args.keep_workspace)
            self.assertTrue(runner.parse_args(["--keep-workspace"])[1].keep_workspace)
            load.assert_not_called()

    def test_dotenv_is_anchored_to_repository_not_working_directory(self):
        (self.root / ".env").write_text("EVAL_MODEL=repo-model\n", encoding="utf-8-sig")
        other = self.root / "elsewhere"
        other.mkdir()
        (other / ".env").write_text("EVAL_MODEL=wrong-model\n", encoding="utf-8")
        previous = Path.cwd()
        try:
            os.chdir(other)
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(runner, "PROJECT_ROOT", self.root),
            ):
                self.assertEqual("repo-model", runner.parse_args([])[1].model)
        finally:
            os.chdir(previous)

    def test_ollama_generic_configuration_and_cli_overrides(self):
        (self.root / ".env").write_text(
            "EVAL_BASE_URL=http://localhost:11434/v1\nEVAL_API_KEY=ollama\nEVAL_MODEL=qwen3:8b\n",
            encoding="utf-8",
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(runner, "PROJECT_ROOT", self.root),
        ):
            _, args = runner.parse_args([])
            self.assertEqual(
                ("http://localhost:11434/v1", "ollama", "qwen3:8b"),
                (args.base_url, args.api_key, args.model),
            )
            _, args = runner.parse_args(
                [
                    "--base-url",
                    "http://remote:11434/v1",
                    "--api-key",
                    "proxy-key",
                    "--model",
                    "my-custom-model:latest",
                ]
            )
            self.assertEqual(
                ("http://remote:11434/v1", "proxy-key", "my-custom-model:latest"),
                (args.base_url, args.api_key, args.model),
            )

    def test_generic_and_legacy_aliases_respect_configuration_layers(self):
        for file_prefix, shell_prefix in [
            ("EVAL", "EVAL"),
            ("LM_STUDIO", "EVAL"),
            ("EVAL", "LM_STUDIO"),
            ("LM_STUDIO", "LM_STUDIO"),
        ]:
            with self.subTest(file_prefix=file_prefix, shell_prefix=shell_prefix):
                file_url = "EVAL_BASE_URL" if file_prefix == "EVAL" else "LM_STUDIO_URL"
                shell_url = "EVAL_BASE_URL" if shell_prefix == "EVAL" else "LM_STUDIO_URL"
                (self.root / ".env").write_text(
                    f"{file_url}=http://file/v1\n{file_prefix}_API_KEY=file-key\n",
                    encoding="utf-8",
                )
                environment = {shell_url: "http://shell/v1", f"{shell_prefix}_API_KEY": "shell-key"}
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(runner, "PROJECT_ROOT", self.root),
                ):
                    before = dict(os.environ)
                    _, args = runner.parse_args([])
                    self.assertEqual(
                        ("http://shell/v1", "shell-key"), (args.base_url, args.api_key)
                    )
                    self.assertEqual(before, dict(os.environ))

    def test_generic_names_win_over_aliases_within_the_same_layer(self):
        values = {
            "EVAL_BASE_URL": "http://localhost:11434/v1",
            "EVAL_API_KEY": "ollama",
            "LM_STUDIO_URL": "http://localhost:1235/v1",
            "LM_STUDIO_API_KEY": "old-key",
        }
        for layer in ("file", "shell"):
            with self.subTest(layer=layer):
                (self.root / ".env").write_text(
                    "\n".join(f"{key}={value}" for key, value in values.items())
                    if layer == "file"
                    else "",
                    encoding="utf-8",
                )
                with (
                    patch.dict(os.environ, values if layer == "shell" else {}, clear=True),
                    patch.object(runner, "PROJECT_ROOT", self.root),
                ):
                    _, args = runner.parse_args([])
                    self.assertEqual(
                        (values["EVAL_BASE_URL"], "ollama"), (args.base_url, args.api_key)
                    )
