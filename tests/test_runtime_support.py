import hashlib
import json

from src.evaluation.checks import evaluate_state_checks
from src.evaluation.runtime_support import current_turn_messages, read_jsonl, workspace_manifest
from tests.helpers import TemporaryDirectoryTestCase


class RuntimeSupportTests(TemporaryDirectoryTestCase):
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
