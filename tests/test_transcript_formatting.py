import unittest

from src.transcript_formatting import CHECKS, check_glossary, check_row, interpret_check


class TranscriptFormattingTests(unittest.TestCase):
    def test_false_has_check_specific_meanings(self):
        for control, expected in (
            ("file_state_guard_disabled", "not disabled"),
            ("full_write_baseline", "No matching"),
            ("read_mtime_drift", "no usable timestamp"),
            ("display_text_write", "did not flag"),
        ):
            raw, meaning = interpret_check({"control": control, "result": False})
            self.assertEqual(raw, "false")
            self.assertIn(expected, meaning)

    def test_null_measurement_is_not_permission(self):
        _, meaning = interpret_check({"control": "file_version_snapshot", "result": None})
        self.assertIn("No usable", meaning)
        _, meaning = interpret_check(
            {
                "control": "whole_file_overwrite",
                "result": None,
                "result_semantics": "refusal_reason",
            }
        )
        self.assertIn("No refusal", meaning)

    def test_refusal_is_not_assumed_to_be_a_block(self):
        raw, meaning = interpret_check(
            {
                "control": "file_state_staleness",
                "result": "stale",
                "result_semantics": "refusal_reason",
            }
        )
        self.assertEqual(raw, "stale")
        self.assertIn("inspect the tool result", meaning)

    def test_snapshot_keeps_source_link_without_labeling_pass(self):
        row = check_row(
            {"control": "file_version_snapshot", "result": [1, 2, {"hex": "abc"}]},
            "[source](/trace:4)",
        )
        self.assertIn("Snapshot available", row)
        self.assertIn("[source](/trace:4)", row)
        self.assertNotIn("PASS", row)

    def test_exception_and_missing_completion(self):
        self.assertEqual(
            interpret_check({"event": "native_control_exception", "exception_type": "OSError"})[0],
            "OSError",
        )
        self.assertEqual(
            interpret_check({"event": "native_control_start"})[0], "No recorded result"
        )

    def test_unknown_type_is_not_guessed(self):
        raw, meaning = interpret_check({"control": "future_check", "result": {"some": "value"}})
        self.assertIn('"some": "value"', raw)
        self.assertIn("no additional interpretation", meaning)

    def test_literal_message_cannot_break_table_or_inject_html(self):
        row = check_row(
            {
                "control": "whole_file_overwrite",
                "result": "A|B\n<script>`x`",
                "result_semantics": "refusal_reason",
            },
            "[source](/trace:1)",
        )
        self.assertEqual(row.count("|"), 5)
        self.assertIn("A&#124;B<br>&lt;script&gt;&#96;x&#96;", row)

    def test_glossary_has_each_check_once(self):
        glossary = check_glossary()
        for control in CHECKS:
            self.assertEqual(glossary.count(f"| `{control}` |"), 1)


if __name__ == "__main__":
    unittest.main()
