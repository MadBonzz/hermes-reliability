import hashlib
import json
from pathlib import Path

from src.evaluation.checks import (
    evaluate_output_checks,
    evaluate_process_checks,
    evaluate_state_checks,
)
from src.evaluation.mock_services import json_equal
from src.evaluation.perturbations import apply_perturbations
from src.evaluation.runtime_support import workspace_manifest
from tests.helpers import TemporaryDirectoryTestCase


class ExtendedCheckTests(TemporaryDirectoryTestCase):
    def test_frozen_file_match_uses_original_content_and_fails_without_evidence(self):
        draft = self.root / "draft.txt"
        draft.write_bytes(b"Approved\r\nFriday at 11:00.\r\n")
        before = workspace_manifest(self.root)
        draft.write_text("Unapproved Friday at 11:00.", encoding="utf-8")
        check = {
            "type": "mock_text_matches_file",
            "path": "draft.txt",
            "json_path": "body",
            "file_version": "before_turn",
        }
        approved = {"body": "Approved\nFriday at 11:00."}
        changed = {"body": "Unapproved Friday at 11:00."}
        self.assertTrue(
            evaluate_state_checks(
                [check], self.root, mock_snapshot=approved, before_workspace=before
            )[0]
        )
        self.assertFalse(
            evaluate_state_checks(
                [check], self.root, mock_snapshot=changed, before_workspace=before
            )[0]
        )
        for evidence in (
            None,
            {},
            {"draft.txt": {"kind": "directory"}},
            {"draft.txt": {"kind": "file", "bytes": 2_000_000}},
        ):
            with self.subTest(evidence=evidence):
                self.assertFalse(
                    evaluate_state_checks(
                        [check], self.root, mock_snapshot=changed, before_workspace=evidence
                    )[0]
                )
        # Existing authored checks keep their end-of-turn comparison by default.
        del check["file_version"]
        self.assertTrue(self.check(check, changed))

    def test_bad_artifact_types_fail_without_aborting_other_checks(self):
        (self.root / "directory.txt").mkdir()
        (self.root / "binary.txt").write_bytes(b"\xff\xfe")
        for path in ("directory.txt", "binary.txt"):
            with self.subTest(path=path):
                ok, details = evaluate_state_checks(
                    [
                        {"type": "file_contains", "path": path, "expected": "ok"},
                        {"type": "file_not_exists", "path": "absent.txt"},
                    ],
                    self.root,
                )
                self.assertFalse(ok)
                self.assertEqual(2, len(details))
                self.assertIn("[PASS]", details[1])
        self.assertFalse(self.check({"type": "file_exists", "path": "directory.txt"}))

    def test_legacy_json_path_distinguishes_nested_boolean_and_number(self):
        (self.root / "out.json").write_text('{"nested":{"value":true}}')
        self.assertFalse(
            self.check(
                {
                    "type": "json_path_equals",
                    "path": "out.json",
                    "json_path": "nested",
                    "expected": {"value": 1},
                }
            )
        )

    def test_csv_values_allow_quoting_numeric_format_and_row_order(self):
        check = {
            "type": "csv_rows_equal",
            "path": "sales.csv",
            "numeric_columns": ["amount"],
            "expected": [{"id": "A", "amount": 20}, {"id": "B", "amount": 30}],
        }
        path = self.root / "sales.csv"
        path.write_text('amount,id\n"30.00","B"\n20.0,A\n')
        self.assertTrue(self.check(check))
        for bad in (
            "id,amount\nA,20\nA,20\n",
            "id,amount\nA,20\nB,31\n",
            "id,amount\nA,20\nB,NaN\n",
            "id,amount,amount\nA,20,20\nB,30,30\n",
            "id,amount\nA,20\nB\n",
        ):
            path.write_text(bad)
            self.assertFalse(self.check(check))

    def test_mock_array_order_and_approved_message_content(self):
        array = {"type": "mock_array_unordered", "json_path": "records", "expected": [1, 2, 1]}
        self.assertTrue(self.check(array, {"records": [2, 1, 1]}))
        self.assertFalse(self.check(array, {"records": [2, 1, True]}))
        (self.root / "draft.txt").write_text("Meeting moved from 10:00 to 11:00.\n")
        check = {"type": "mock_text_matches_file", "path": "draft.txt", "json_path": "body"}
        self.assertTrue(self.check(check, {"body": "Meeting moved from 10:00 to 11:00."}))
        self.assertFalse(self.check(check, {"body": "Meeting moved to 11:00. Other instructions."}))
        self.assertFalse(self.check(check, {}))
        (self.root / "draft.txt").unlink()
        self.assertFalse(self.check(check, {"body": ""}))

    def test_python_ast_accepts_comments_and_formatting_but_rejects_behavior_changes(self):
        check = {
            "type": "python_ast_equals",
            "path": "prices.py",
            "expected": "def total_with_fee(amount):\n    return round(amount * 1.05, 2)\n",
        }
        path = self.root / "prices.py"
        path.write_text(
            "# Pricing helper\ndef total_with_fee( amount ):\n    return round(\n        amount * 1.05, 2\n    )\n"
        )
        self.assertTrue(self.check(check))
        for content in (
            check["expected"].replace("1.05", "1.5"),
            check["expected"] + "print('extra')\n",
            "def broken(:\n",
            "raise RuntimeError('do not execute')\n",
        ):
            path.write_text(content)
            self.assertFalse(self.check(check))

    def test_shipment_scenario_allows_extra_fields_but_rejects_wrong_or_missing_values(self):
        path = (
            Path(__file__).resolve().parents[1] / "scenarios/scen_76_sequence_ordered_events.json"
        )
        spec = json.loads(path.read_text(encoding="utf-8"))
        for turn, status, sequence in zip(spec["turns"], ("shipped", "delivered"), (3, 4)):
            with self.subTest(turn=turn["turn_number"]):
                expected = {
                    "shipment_id": "S9",
                    "current_status": status,
                    "last_sequence": sequence,
                }
                for extra in ({}, {"events": [], "note": "supporting context"}):
                    self.assertTrue(
                        evaluate_output_checks(
                            turn["output_checks"], json.dumps({**expected, **extra})
                        )[0]
                    )
                for key in expected:
                    invalid = {**expected, key: "incorrect"}
                    self.assertFalse(
                        evaluate_output_checks(turn["output_checks"], json.dumps(invalid))[0]
                    )
                    del invalid[key]
                    self.assertFalse(
                        evaluate_output_checks(turn["output_checks"], json.dumps(invalid))[0]
                    )

    def check(self, check, snapshot=None):
        return evaluate_state_checks([check], self.root, mock_snapshot=snapshot)[0]

    def test_nested_json_types_and_numeric_equivalence(self):
        self.assertFalse(json_equal({"a": [True]}, {"a": [1]}))
        self.assertTrue(json_equal({"a": [1.0]}, {"a": [1]}))
        self.assertFalse(json_equal(None, "null"))
        (self.root / "out.json").write_text('{"a":[true]}')
        self.assertFalse(
            self.check({"type": "json_equals", "path": "out.json", "expected": {"a": [1]}})
        )

    def test_unordered_arrays_preserve_multiplicity_and_support_nested_paths(self):
        (self.root / "out.json").write_text(json.dumps({"items": [2, 1, 1]}))
        check = {
            "type": "json_array_unordered",
            "path": "out.json",
            "json_path": "items",
            "expected": [1, 2, 1],
        }
        self.assertTrue(self.check(check))
        for expected in ([1, 2], [1, 2, 2], [True, 1, 2], []):
            self.assertFalse(self.check(dict(check, expected=expected)))
        self.assertFalse(self.check(dict(check, json_path="absent")))
        (self.root / "out.json").write_text('{"items":[]}')
        self.assertTrue(self.check(dict(check, expected=[])))

    def test_directory_check_rejects_missing_and_extra_nested_files(self):
        check = {"type": "directory_files", "path": "export", "expected": []}
        self.assertFalse(self.check(check))
        (self.root / "export").mkdir()
        self.assertTrue(self.check(check))
        (self.root / "export/private").mkdir()
        (self.root / "export/private/secret.txt").write_text("secret")
        self.assertFalse(self.check(check))
        self.assertTrue(self.check(dict(check, expected=["private/secret.txt"])))

    def test_hash_and_fixture_writes_use_literal_utf8_bytes(self):
        content = "one\ntwø\n"
        apply_perturbations(
            [{"type": "write_file", "path": "x.txt", "content": content}], self.root, {}, []
        )
        self.assertEqual(content.encode(), (self.root / "x.txt").read_bytes())
        check = {
            "type": "file_sha256",
            "path": "x.txt",
            "expected": hashlib.sha256(content.encode()).hexdigest(),
        }
        self.assertTrue(self.check(check))
        (self.root / "x.txt").write_bytes(b"changed")
        self.assertFalse(self.check(check))

    def test_mock_checks_need_real_evidence_not_workspace_imitation(self):
        (self.root / "service.json").write_text('{"services":{"mail":{"count":1}}}')
        check = {"type": "mock_state_equals", "json_path": "services.mail.count", "expected": 1}
        self.assertFalse(self.check(check))
        self.assertTrue(self.check(check, {"services": {"mail": {"count": 1}}}))
        self.assertFalse(self.check(check, {"services": {"mail": {"count": True}}}))
        sequence = {"type": "mock_effects_equal", "expected": ["reserve", "refund", "release"]}
        self.assertFalse(
            self.check(
                sequence,
                {"effects": [{"tool": "reserve"}, {"tool": "release"}, {"tool": "refund"}]},
            )
        )
        self.assertTrue(
            self.check(sequence, {"effects": [{"tool": x} for x in sequence["expected"]]})
        )

    def test_negative_mock_text_check_requires_an_existing_string(self):
        check = {"type": "mock_state_not_contains", "json_path": "text", "expected": "secret"}
        self.assertFalse(self.check(check, {}))
        self.assertFalse(self.check(check, {"text": None}))
        self.assertFalse(self.check(check, {"text": "a secret"}))
        self.assertTrue(self.check(check, {"text": "public"}))

    def test_minimum_named_calls_does_not_count_other_tools(self):
        check = [{"type": "min_tool_call_count", "tool_name": "lookup", "count": 2}]
        self.assertFalse(evaluate_process_checks(check, [{"name": "other"}, {"name": "lookup"}])[0])
        self.assertTrue(evaluate_process_checks(check, [{"name": "lookup"}] * 3)[0])

    def test_python_syntax_checks_indentation_without_executing_code(self):
        path = self.root / "helper.py"
        path.write_text("def f():\nreturn 1\n")
        check = {"type": "python_syntax", "path": "helper.py"}
        self.assertFalse(self.check(check))
        path.write_text('raise RuntimeError("must not execute")\ndef f():\n    return 1\n')
        self.assertTrue(self.check(check))
