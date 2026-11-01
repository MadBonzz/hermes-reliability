from src import checks
from tests.helpers import TemporaryDirectoryTestCase


class CheckTests(TemporaryDirectoryTestCase):
    def test_ambiguous_or_nonfinite_json_fails_all_json_artifact_checks(self):
        state_checks = [
            {"type": "json_valid", "path": "state.json"},
            {"type": "json_equals", "path": "state.json", "json_path": "count", "expected": 10},
            {
                "type": "json_path_equals",
                "path": "state.json",
                "json_path": "count",
                "expected": 10,
            },
            {"type": "json_key_count", "path": "state.json", "expected": 2},
        ]
        output_checks = [{"type": "json_answer", "json_path": "count", "expected": 10}]
        for value in (
            '{"count": 9, "count": 10, "extra": null}',
            '{"count": 10, "extra": {"id": "wrong", "id": "right"}}',
            '{"count": 10, "extra": NaN}',
            '{"count": 10, "extra": Infinity}',
            '{"count": 10, "extra": -Infinity}',
            '{"count": 10, "extra": 1e999}',
        ):
            (self.root / "state.json").write_text(value, encoding="utf-8")
            with self.subTest(value=value):
                for check in state_checks:
                    self.assertFalse(checks.evaluate_state_checks([check], self.root)[0])
                self.assertFalse(checks.evaluate_output_checks(output_checks, value)[0])
        valid = '{"count": 1e1, "extra": {"other": 2.5}}'
        (self.root / "state.json").write_text(valid, encoding="utf-8")
        self.assertTrue(checks.evaluate_state_checks(state_checks, self.root)[0])
        self.assertTrue(checks.evaluate_output_checks(output_checks, valid)[0])

    def test_tool_order_allows_preflight_read_and_requires_later_verification(self):
        check = [{"type": "tool_called_before", "first_tool": "claim", "second_tool": "list"}]
        for names, expected in [
            (["list", "claim", "list"], True),
            (["claim", "list"], True),
            (["list", "claim"], False),
            (["list"], False),
            (["claim"], False),
            ([], False),
        ]:
            with self.subTest(names=names):
                calls = [{"name": name, "arguments": "{}"} for name in names]
                self.assertEqual(expected, checks.evaluate_process_checks(check, calls)[0])

    def test_tool_order_requires_distinct_occurrences_for_same_tool(self):
        check = [{"type": "tool_called_before", "first_tool": "read", "second_tool": "read"}]
        for names, expected in [(["read"], False), (["read", "read"], True)]:
            with self.subTest(names=names):
                self.assertEqual(
                    expected,
                    checks.evaluate_process_checks(
                        check, [{"name": name, "arguments": "{}"} for name in names]
                    )[0],
                )

    def test_boolean_assertion_does_not_accept_number(self):
        (self.root / "state.json").write_text('{"ok": 1}')
        ok, _ = checks.evaluate_state_checks(
            [
                {
                    "type": "json_path_equals",
                    "path": "state.json",
                    "json_path": "ok",
                    "expected": True,
                }
            ],
            self.root,
        )
        self.assertFalse(ok)
