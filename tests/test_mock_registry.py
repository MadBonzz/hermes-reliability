import json

from tests.helpers import RegistryTestCase, mock


class MockRegistryTests(RegistryTestCase):
    def test_cleanup_without_snapshot_is_noop(self):
        before = dict(self.registry._tools)
        self.make_mock_manager(self.root, {}).cleanup()
        self.assertEqual(before, self.registry._tools)

    def test_mock_no_mock_mock_registry_restoration_and_cache_invalidation(self):
        before = dict(self.registry._tools)
        for specs in ([mock("first")], [], [mock("second")]):
            manager = self.make_mock_manager(self.root, {})
            manager.snapshot_registry()
            manager.register_scenario_mocks(specs)
            self.assertEqual(set(self.registry._tools), set(before) | {m["name"] for m in specs})
            self.toolsets.CORE_TOOLS.add("temporary")
            generation = self.registry._generation
            manager.cleanup()
            self.assertEqual(before, self.registry._tools)
            self.assertEqual({"terminal"}, self.toolsets.CORE_TOOLS)
            self.assertGreater(self.registry._generation, generation)
            self.assertEqual({}, self.cached)
            self.assertEqual({}, self.toolsets._resolve_toolset_memo)

    def test_override_restores_original_entry_identity(self):
        original = self.registry._tools["terminal"]
        manager = self.make_mock_manager(self.root, {})
        manager.register_scenario_mocks([mock("terminal")])
        manager.cleanup()
        self.assertIs(original, self.registry._tools["terminal"])

    def test_registration_failure_restores_partial_changes(self):
        manager = self.make_mock_manager(self.root, {})
        with self.assertRaises(KeyError):
            manager.register_scenario_mocks([mock(), {}])
        manager.cleanup()
        self.assertNotIn("mock", self.registry._tools)

    def test_append_timeout_commits_once_and_clears_flag(self):
        flags = {"fail": True}
        manager = self.make_mock_manager(self.root, flags)
        handler = manager._build_handler(
            {
                "behavior": {
                    "type": "state_append_and_optional_error",
                    "target_file": "credits.json",
                    "error_when_flag_set": "fail",
                    "clear_flag_after_first_error": True,
                }
            }
        )
        self.assertEqual("error", json.loads(handler({"amount": 50}))["status"])
        self.assertEqual([{"amount": 50}], json.loads((self.root / "credits.json").read_text()))
        self.assertFalse(flags["fail"])

    def test_transient_error_before_write_does_not_commit(self):
        flags = {"fail": True}
        manager = self.make_mock_manager(self.root, flags)
        handler = manager._build_handler(
            {
                "behavior": {
                    "type": "state_append_and_optional_error",
                    "target_file": "db.json",
                    "error_when_flag_set": "fail",
                    "clear_flag_after_first_error": True,
                    "fail_before_write": True,
                }
            }
        )
        handler({"record": "x"})
        self.assertFalse((self.root / "db.json").exists())
        handler({"record": "x"})
        self.assertEqual([{"record": "x"}], json.loads((self.root / "db.json").read_text()))

    def test_corrupt_mock_state_is_not_silently_discarded(self):
        target = self.root / "state.json"
        target.write_text('[{"record": "valuable"')
        manager = self.make_mock_manager(self.root, {})
        handler = manager._build_handler(
            {"behavior": {"type": "state_append_and_optional_error", "target_file": "state.json"}}
        )
        result = json.loads(handler({"record": "new"}))
        self.assertEqual("error", result["status"])
        self.assertEqual('[{"record": "valuable"', target.read_text())

    def test_key_value_fault_timing_and_one_shot_recovery(self):
        for before in (True, False):
            with self.subTest(fail_before_write=before):
                target = self.root / f"kv_{before}.json"
                target.write_text('{"existing": 7}')
                flags = {"fail": True}
                handler = self.make_mock_manager(self.root, flags)._build_handler(
                    {
                        "behavior": {
                            "type": "state_kv_write_and_optional_error",
                            "target_file": target.name,
                            "error_when_flag_set": "fail",
                            "fail_before_write": before,
                            "clear_flag_after_first_error": True,
                        }
                    }
                )
                self.assertEqual("error", json.loads(handler({"key": "new", "value": 9}))["status"])
                expected = {"existing": 7} if before else {"existing": 7, "new": 9}
                self.assertEqual(expected, json.loads(target.read_text()))
                self.assertFalse(flags["fail"])
                self.assertEqual("ok", json.loads(handler({"key": "new", "value": 9}))["status"])
                self.assertEqual({"existing": 7, "new": 9}, json.loads(target.read_text()))

    def test_static_fault_remains_active_until_explicitly_cleared(self):
        flags = {"offline": True}
        handler = self.make_mock_manager(self.root, flags)._build_handler(
            {
                "behavior": {
                    "type": "static_return",
                    "error_when_flag_set": "offline",
                    "return_payload": {"rate": 1.1},
                }
            }
        )
        for _ in range(2):
            self.assertEqual("error", json.loads(handler({}))["status"])
        self.assertTrue(flags["offline"])
        flags["offline"] = False
        self.assertEqual({"rate": 1.1}, json.loads(handler({})))

    def test_unknown_behavior_is_rejected_instead_of_reporting_success(self):
        with self.assertRaisesRegex(ValueError, "Unknown mock behavior"):
            self.make_mock_manager(self.root, {})._build_handler({"behavior": {"type": "typo"}})
