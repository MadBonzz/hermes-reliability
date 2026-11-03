import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from src.evaluation.checks import evaluate_state_checks
from src.evaluation.runtime_support import (
    current_turn_messages,
    read_jsonl,
    workspace_manifest,
    write_json_atomic,
)
from tests.helpers import TemporaryDirectoryTestCase


class RuntimeSupportTests(TemporaryDirectoryTestCase):
    def test_atomic_checkpoint_retries_windows_denial_without_truncating_old_report(self):
        target = self.root / "report.json"
        write_json_atomic(target, {"old": True})
        replace = Path.replace
        failure = PermissionError("transient Windows lock")
        failure.winerror = 5
        calls = []

        def attempt(temporary, path):
            self.assertEqual(json.loads(target.read_text()), {"old": True})
            calls.append(temporary)
            if len(calls) == 1:
                raise failure
            return replace(temporary, path)

        with (
            patch.object(Path, "replace", attempt),
            patch("src.evaluation.runtime_support.time.sleep") as sleep,
        ):
            write_json_atomic(target, {"new": True})
        sleep.assert_called_once_with(0.05)
        self.assertEqual(json.loads(target.read_text()), {"new": True})
        self.assertFalse(list(target.parent.glob("report.json.*.tmp")))

    def test_atomic_checkpoint_permanent_failure_preserves_old_and_pending_evidence(self):
        target = self.root / "report.json"
        write_json_atomic(target, {"old": True})
        for winerror, attempts in ((5, 5), (32, 5), (33, 5), (None, 1)):
            failure = PermissionError("denied")
            if winerror is not None:
                failure.winerror = winerror
            with (
                self.subTest(winerror=winerror),
                patch.object(Path, "replace", side_effect=failure) as replace,
                patch("src.evaluation.runtime_support.time.sleep") as sleep,
            ):
                with self.assertRaises(PermissionError):
                    write_json_atomic(target, {"pending": True})
                self.assertEqual(replace.call_count, attempts)
                self.assertEqual(sleep.call_count, attempts - 1)
            self.assertEqual(json.loads(target.read_text()), {"old": True})
            pending = list(target.parent.glob("report.json.*.tmp"))
            self.assertTrue(pending)
            self.assertTrue(all(json.loads(p.read_text()) == {"pending": True} for p in pending))
        self.assertEqual(len(pending), 4)
        frozen = {p: p.read_bytes() for p in pending}
        # The parent may publish an error report after a worker's failed write.
        # That update must not reuse/destroy the worker's unpublished checkpoint.
        write_json_atomic(target, {"error": "worker exited"})
        self.assertEqual(frozen, {p: p.read_bytes() for p in pending})

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_atomic_checkpoint_recovers_after_real_windows_reader_releases_handle(self):
        import ctypes
        from ctypes import wintypes

        target = self.root / "report.json"
        write_json_atomic(target, {"old": True})
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(target), 0x80000000, 1, None, 3, 0x80, None)
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value)

        def release(_):
            nonlocal handle
            self.assertEqual(json.loads(target.read_text()), {"old": True})
            self.assertTrue(kernel.CloseHandle(handle))
            handle = None

        try:
            with patch("src.evaluation.runtime_support.time.sleep", side_effect=release) as sleep:
                write_json_atomic(target, {"new": True})
            sleep.assert_called_once_with(0.05)
            self.assertEqual(json.loads(target.read_text()), {"new": True})
        finally:
            if handle is not None:
                kernel.CloseHandle(handle)

    def test_merged_user_boundary_requires_exact_prefix_metadata(self):
        message = {
            "role": "user",
            "content": "summary\n\ncurrent",
            "_merged_turn_prefix": "summary",
        }
        messages = [message, {"role": "assistant", "content": "answer"}]
        self.assertEqual(messages, current_turn_messages(messages, "current"))
        for change in (
            {"_merged_turn_prefix": "other"},
            {"_merged_turn_prefix": ""},
            {"content": "summary\n\nquoted current"},
            {"role": "assistant"},
        ):
            self.assertIsNone(current_turn_messages([{**message, **change}], "current"))
        self.assertIsNone(
            current_turn_messages([{"role": "user", "content": "summary\n\ncurrent"}], "current")
        )

    def test_jsonl_reader_preserves_records_and_rejects_malformed_evidence(self):
        path = self.root / "events.jsonl"
        path.write_bytes(b'{"event":"first"}\r\n{"event":"second"}\r\n')
        self.assertEqual(read_jsonl(path), [{"event": "first"}, {"event": "second"}])
        for content in ('{"event":"first"}\n\n', '{"event":'):
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                read_jsonl(path)

    def test_manifest_retains_empty_directories_for_inventory_replay(self):
        workspace = self.root / "task"
        workspace.mkdir()
        (workspace / "sandbox/nested").mkdir(parents=True)
        (workspace / "input.txt").write_bytes(b"data\n")
        evidence = workspace_manifest(workspace)
        self.assertEqual({"kind": "directory"}, evidence["."])
        self.assertEqual({"kind": "directory"}, evidence["sandbox"])
        self.assertEqual({"kind": "directory"}, evidence["sandbox/nested"])
        self.assertEqual("data\n", evidence["input.txt"]["text"])
        self.assertEqual(hashlib.sha256(b"data\n").hexdigest(), evidence["input.txt"]["sha256"])

        replay = self.root / "replay"
        for name, item in evidence.items():
            target = replay / name
            if item["kind"] == "directory":
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.write_bytes(item["text"].encode("utf-8"))
        checks = [{"type": "directory_files", "path": "sandbox", "expected": []}]
        self.assertTrue(evaluate_state_checks(checks, replay)[0])
        self.assertNotIn("absent", evidence)
        checks[0]["path"] = "absent"
        self.assertFalse(evaluate_state_checks(checks, replay)[0])

    def test_manifest_distinguishes_missing_workspace_from_empty_workspace(self):
        workspace = self.root / "task"
        self.assertEqual({}, workspace_manifest(workspace))
        workspace.mkdir()
        self.assertEqual({".": {"kind": "directory"}}, workspace_manifest(workspace))

    def test_turn_boundary_ignores_old_history_length(self):
        messages = [
            {"role": "system", "content": "summary"},
            {"role": "user", "content": "current"},
            {"role": "assistant", "content": "done"},
        ]
        self.assertEqual(messages[1:], current_turn_messages(messages, "current"))
