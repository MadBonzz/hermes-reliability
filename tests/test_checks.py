from src.evaluation import checks
from tests.helpers import TemporaryDirectoryTestCase


class CheckTests(TemporaryDirectoryTestCase):
    def test_native_outputs_require_explicit_root_and_do_not_alias_workspace(self):
        workspace, outputs = self.root / "workspace", self.root / "outputs"
        workspace.mkdir()
        outputs.mkdir()
        (workspace / "report.txt").write_text("wrong workspace copy")
        (outputs / "report.txt").write_text("correct deliverable")
        check = {
            "type": "file_exact_lines",
            "path": "report.txt",
            "expected": ["correct deliverable"],
            "artifact_scope": "outputs",
        }
        self.assertFalse(checks.evaluate_state_checks([check], workspace)[0])
        passed, details = checks.evaluate_state_checks(
            [check], workspace, artifact_roots={"outputs": outputs}
        )
        self.assertTrue(passed, details)
        self.assertIn("artifact_scope=outputs", details[0])
        ordinary = {k: v for k, v in check.items() if k != "artifact_scope"}
        self.assertFalse(
            checks.evaluate_state_checks(
                [ordinary], workspace, artifact_roots={"outputs": outputs}
            )[0]
        )
        before = {"report.txt": {"kind": "file", "text": "approved draft"}}
        compare = {
            "type": "mock_text_matches_file",
            "path": "report.txt",
            "json_path": "body",
            "file_version": "before_turn",
            "artifact_scope": "outputs",
        }
        kwargs = {
            "artifact_roots": {"outputs": outputs},
            "mock_snapshot": {"body": "approved draft"},
            "before_workspace": before,
        }
        self.assertFalse(checks.evaluate_state_checks([compare], workspace, **kwargs)[0])
        self.assertTrue(
            checks.evaluate_state_checks(
                [compare], workspace, before_artifacts={"outputs": before}, **kwargs
            )[0]
        )
        check["path"] = "../workspace/report.txt"
        self.assertFalse(
            checks.evaluate_state_checks([check], workspace, artifact_roots={"outputs": outputs})[0]
        )

    def test_settlement_contract_reports_status_identity_and_duplicate_failures(self):
        import copy
        import json

        invoice = {"invoice_id": "INV-1", "recipient": "Acme", "amount_usd": 50}
        check = {
            "type": "settlement_files_match",
            "path": "receipts",
            "service": "payments",
            "expected": [invoice],
        }
        snapshot = {
            "services": {"payments": {"records": [invoice]}},
            "receipts": [
                {
                    "service": "payments",
                    "arguments": invoice,
                    "response": {"status": "ok", "receipt_id": "R1"},
                }
            ],
        }
        (self.root / "receipts").mkdir()
        artifact = self.root / "receipts/INV-1.json"
        valid = {**invoice, "status": "paid", "receipt_id": "R1"}
        for label, changes, expected in (
            ("valid", {}, True),
            ("service status is not the reimbursement contract", {"status": "ok"}, False),
            ("wrong receipt", {"receipt_id": "R2"}, False),
            ("boolean amount", {"amount_usd": True}, False),
        ):
            with self.subTest(label=label):
                artifact.write_text(json.dumps({**valid, **changes}), encoding="utf-8")
                passed, details = checks.evaluate_state_checks(
                    [check], self.root, mock_snapshot=snapshot
                )
                self.assertEqual(passed, expected)
                diagnostic = next(x for x in details if "receipts/INV-1.json:" in x)
                self.assertIn("got ", diagnostic)
                self.assertIn("expected ", diagnostic)
                self.assertIn("'status': 'paid'", diagnostic)
        artifact.write_text(json.dumps(valid), encoding="utf-8")
        duplicate = copy.deepcopy(snapshot)
        duplicate["services"]["payments"]["records"].append(invoice)
        passed, details = checks.evaluate_state_checks([check], self.root, mock_snapshot=duplicate)
        self.assertFalse(passed)
        self.assertTrue(any(x.startswith("[FAIL] settlement ledger records") for x in details))
        self.assertTrue(any(x.startswith("[PASS] settlement receipts/INV-1.json") for x in details))

    def test_nested_path_diagnostics_do_not_alias_same_named_root_file(self):
        (self.root / "brief.md").write_text("Correct brief", encoding="utf-8")
        nested = [
            {"type": "file_exists", "path": "workspace/brief.md"},
            {"type": "file_contains", "path": "workspace/brief.md", "expected": "Correct"},
        ]
        passed, details = checks.evaluate_state_checks(nested, self.root)
        self.assertFalse(passed)
        self.assertTrue(all("workspace/brief.md" in detail for detail in details))
        self.assertTrue(all(detail.startswith("[FAIL]") for detail in details))
        root_checks = [{**check, "path": "brief.md"} for check in nested]
        self.assertTrue(checks.evaluate_state_checks(root_checks, self.root)[0])
        (self.root / "workspace").mkdir()
        (self.root / "workspace/brief.md").write_text("Correct brief", encoding="utf-8")
        self.assertTrue(checks.evaluate_state_checks(nested, self.root)[0])

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
