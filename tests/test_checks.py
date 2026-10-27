from src import checks
from tests.helpers import TemporaryDirectoryTestCase


class CheckTests(TemporaryDirectoryTestCase):
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
