import os
import unittest
from types import SimpleNamespace

from src.perturbations import apply_perturbations
from tests.helpers import TemporaryDirectoryTestCase


class PerturbationTests(TemporaryDirectoryTestCase):
    def test_native_noop_is_logged_as_truncation(self):
        compressor = SimpleNamespace(
            protect_first_n=3, protect_last_n=4, compress=lambda history: history
        )
        events, history = [], [{"role": "user", "content": "old"}]
        apply_perturbations(
            [{"type": "compress_context", "use_native_compressor": True}],
            self.root,
            {},
            history,
            SimpleNamespace(context_compressor=compressor),
            events,
        )
        self.assertEqual("history_truncation", events[0]["applied_method"])
        self.assertNotEqual("old", history[0]["content"])
        self.assertEqual((3, 4), (compressor.protect_first_n, compressor.protect_last_n))

    def test_native_failure_restores_thresholds(self):
        def fail(history):
            raise RuntimeError("offline")

        compressor = SimpleNamespace(protect_first_n=3, protect_last_n=4, compress=fail)
        apply_perturbations(
            [{"type": "compress_context", "use_native_compressor": True}],
            self.root,
            {},
            [],
            SimpleNamespace(context_compressor=compressor),
        )
        self.assertEqual((3, 4), (compressor.protect_first_n, compressor.protect_last_n))

    @unittest.skipUnless(os.name == "nt", "Windows permission injection")
    def test_windows_file_permission_fault_and_cleanup(self):
        target = self.root / "locked.txt"
        target.write_text("original")
        try:
            apply_perturbations(
                [{"type": "chmod", "path": "locked.txt", "mode": "0444"}], self.root, {}, []
            )
            with self.assertRaises(PermissionError):
                target.write_text("changed")
        finally:
            apply_perturbations(
                [{"type": "chmod", "path": "locked.txt", "mode": "0644"}], self.root, {}, []
            )
        target.write_text("unlocked")
        self.assertEqual("unlocked", target.read_text())

    @unittest.skipUnless(os.name == "nt", "Windows permission injection")
    def test_windows_directory_permission_fault_and_cleanup(self):
        target = self.root / "backup"
        target.mkdir()
        try:
            apply_perturbations(
                [{"type": "chmod", "path": "backup", "mode": "0555"}], self.root, {}, []
            )
            self.assertEqual([], list(target.iterdir()))
            with self.assertRaises(PermissionError):
                (target / "blocked.txt").write_text("blocked")
        finally:
            apply_perturbations(
                [{"type": "chmod", "path": "backup", "mode": "0755"}], self.root, {}, []
            )
        (target / "allowed.txt").write_text("unlocked")
        self.assertEqual("unlocked", (target / "allowed.txt").read_text())

    def test_native_compression_preserves_disk_and_restores_settings(self):
        def compress(history, force=False):
            self.assertTrue(force)
            self.assertEqual((0, 0), (compressor.protect_first_n, compressor.protect_last_n))
            history[:] = [{"role": "system", "content": "native summary"}]
            return history

        compressor = SimpleNamespace(protect_first_n=3, protect_last_n=4, compress=compress)
        original = [{"role": "user", "content": "saved rules"}]
        history = list(original)
        (self.root / "rules.json").write_text('{"limit": 10}')
        events = []
        apply_perturbations(
            [{"type": "compress_context", "use_native_compressor": True}],
            self.root,
            {},
            history,
            SimpleNamespace(context_compressor=compressor),
            events,
        )
        self.assertEqual("native_compression", events[0]["applied_method"])
        self.assertEqual([{"role": "system", "content": "native summary"}], history)
        self.assertEqual([{"role": "user", "content": "saved rules"}], original)
        self.assertEqual('{"limit": 10}', (self.root / "rules.json").read_text())
        self.assertEqual((3, 4), (compressor.protect_first_n, compressor.protect_last_n))

    def test_truncation_removes_orphaned_tool_replies(self):
        history = [
            {"role": "user", "content": "old"},
            {"role": "tool", "content": "orphaned"},
            {"role": "assistant", "content": "recent"},
        ]
        apply_perturbations(
            [{"type": "truncate_history", "keep_last_n_messages": 2}],
            self.root,
            {},
            history,
        )
        self.assertEqual([{"role": "assistant", "content": "recent"}], history)
