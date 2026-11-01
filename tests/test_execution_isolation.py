import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from src.execution_isolation import FILE_TOOLS, file_tool_guard, guarded_arguments, isolated_python
from tests.helpers import TemporaryDirectoryTestCase


class PathGuardTests(TemporaryDirectoryTestCase):
    def test_python_environment_routes_installers_and_restores_on_error(self):
        def create(environment):
            scripts = environment / ("Scripts" if os.name == "nt" else "bin")
            scripts.mkdir(parents=True)
            (scripts / ("python.exe" if os.name == "nt" else "python")).write_bytes(b"launcher")

        with (
            patch.dict(os.environ, {"PIP_TARGET": "outside", "PYTHONPATH": "host-libraries"}),
            patch("src.execution_isolation.venv.EnvBuilder") as builder,
        ):
            original = dict(os.environ)
            builder.return_value.create.side_effect = create
            with (
                self.assertRaisesRegex(RuntimeError, "test exit"),
                isolated_python(self.root) as info,
            ):
                self.assertFalse(info["system_site_packages"])
                self.assertEqual(info["python"], os.environ["UV_PYTHON"])
                self.assertEqual("true", os.environ["PIP_REQUIRE_VIRTUALENV"])
                self.assertNotIn("PIP_TARGET", os.environ)
                self.assertNotIn("PYTHONPATH", os.environ)
                self.assertTrue(os.environ["PATH"].startswith(str(self.root / "python_env")))
                raise RuntimeError("test exit")
            self.assertEqual(original, dict(os.environ))
            builder.assert_called_once_with(with_pip=True, system_site_packages=False)

    def test_relative_paths_follow_task_cwd_without_allowing_escape(self):
        nested = self.root / "nested"
        nested.mkdir()
        args = guarded_arguments(
            "write_file", {"path": "a.txt", "content": "ok"}, self.root, nested
        )
        self.assertEqual(str(nested / "a.txt"), args["path"])
        with self.assertRaises(ValueError):
            guarded_arguments("read_file", {"path": "../../private"}, self.root, nested)

    def test_normal_operations_and_handler_restoration(self):
        observed = []
        entries = {
            name: SimpleNamespace(handler=lambda a, **kw: observed.append(a)) for name in FILE_TOOLS
        }
        registry = SimpleNamespace(_tools=dict(entries))
        with file_tool_guard(registry, self.root) as evidence:
            registry._tools["write_file"].handler({"path": "nested/report.json", "content": "{}"})
            self.assertEqual(str(self.root / "nested/report.json"), observed[0]["path"])
            denied = registry._tools["read_file"].handler({"path": "../eval_report.json"})
            self.assertEqual("EVAL_PATH_DENIED", json.loads(denied)["code"])
            self.assertEqual(1, len(observed))
            self.assertEqual("evaluation_harness", evidence["denials"][0]["enforced_by"])
        for name, entry in entries.items():
            self.assertIs(entry, registry._tools[name])

    def test_all_patch_paths_including_moves_are_checked(self):
        for header in ("Update File", "Add File", "Delete File", "Move to"):
            with self.subTest(header=header), self.assertRaises(ValueError):
                guarded_arguments(
                    "patch",
                    {
                        "mode": "patch",
                        "patch": f"*** Begin Patch\n*** Update File: good.txt\n*** {header}: ../secret\n*** End Patch",
                    },
                    self.root,
                )

    def test_recursive_search_rejects_outward_link(self):
        outside = self.root.parent / (self.root.name + "_outside")
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        link = self.root / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink privilege unavailable")
        self.addCleanup(link.unlink)
        with self.assertRaises(ValueError):
            guarded_arguments("search_files", {"pattern": "secret"}, self.root)

    def test_native_move_and_whitespace_patch_headers(self):
        for body in ("***Move File: a -> ../private", "***  Update   File: ../private"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                guarded_arguments("patch", {"mode": "patch", "patch": body}, self.root)
        result = guarded_arguments(
            "patch", {"mode": "patch", "patch": "*** Move File: a -> b"}, self.root
        )
        self.assertIn(str(self.root / "b"), result["patch"])

    def test_hardlink_denied_and_regular_read_allowed(self):
        source = self.root / "data"
        source.write_text("ok")
        self.assertEqual(
            str(source), guarded_arguments("read_file", {"path": "data"}, self.root)["path"]
        )
        os.link(source, self.root / "alias")
        with self.assertRaises(ValueError):
            guarded_arguments("read_file", {"path": "alias"}, self.root)

    def test_terminal_is_not_modified(self):
        terminal = SimpleNamespace(handler=object())
        registry = SimpleNamespace(
            _tools={name: SimpleNamespace(handler=lambda a: a) for name in FILE_TOOLS}
        )
        registry._tools["terminal"] = terminal
        with file_tool_guard(registry, self.root):
            self.assertIs(terminal, registry._tools["terminal"])
