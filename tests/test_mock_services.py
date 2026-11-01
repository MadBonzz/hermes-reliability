import concurrent.futures
import copy
import json

from src.perturbations import apply_perturbations
from tests.helpers import RegistryTestCase


class MockServiceTests(RegistryTestCase):
    def test_replay_survives_precondition_changes_and_active_fault(self):
        pay = self.handler(
            requires={"ready": True},
            initial_state={"ready": True, "records": []},
            updates={"ready": False},
            error_when_flag_set="offline",
        )
        args = {"request_id": "one", "amount": 25}
        original = json.loads(pay(args))
        self.manager.fault_flags["offline"] = True
        self.assertEqual({**original, "replayed": True}, json.loads(pay(args)))
        self.assertEqual("IDEMPOTENCY_CONFLICT", json.loads(pay({**args, "amount": 30}))["code"])
        self.assertEqual(1, len(self.manager.services.snapshot()["effects"]))

    def test_precondition_rejection_is_not_masked_by_transient_failure(self):
        pay = self.handler(
            requires={"ready": True},
            initial_state={"ready": False, "records": []},
            transient_failures=2,
            failure_response={"status": "pending"},
        )
        args = {"request_id": "one", "amount": 25}
        self.assertEqual("PRECONDITION_FAILED", json.loads(pay(args))["code"])
        self.manager.services.patch("bank", {"ready": True})
        self.assertEqual("pending", json.loads(pay(args))["status"])
        self.assertEqual("ok", json.loads(pay(args))["status"])

    def test_argument_identity_and_response_templates_before_mutation(self):
        pay = self.handler(
            requires_arguments={"request_id": "known"},
            return_payload={"request_id": {"$arg": "request_id"}},
        )
        self.assertEqual(
            "PRECONDITION_FAILED", json.loads(pay({"request_id": "unknown", "amount": 25}))["code"]
        )
        self.assertEqual([], self.manager.services.snapshot()["effects"])
        self.assertEqual(
            {"request_id": "known"}, json.loads(pay({"request_id": "known", "amount": 25}))
        )
        broken = self.handler(name="broken", return_payload={"value": {"$arg": "missing"}})
        before = self.manager.services.snapshot()["services"]
        self.assertIn(
            "Missing argument", json.loads(broken({"request_id": "a", "amount": 25}))["error"]
        )
        self.assertEqual(before, self.manager.services.snapshot()["services"])
        self.assertEqual(1, len(self.manager.services.snapshot()["effects"]))

    def setUp(self):
        super().setUp()
        self.manager = self.make_mock_manager(self.root, {})

    def handler(self, name="pay", operation="idempotent_append", **extra):
        parameters = {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "minLength": 1},
                "amount": {"type": "integer"},
            },
            "required": ["request_id", "amount"],
            "additionalProperties": False,
        }
        behavior = {
            "type": "stateful_service",
            "service": "bank",
            "operation": operation,
            "initial_state": {"records": []},
            "idempotency_field": "request_id",
            **extra,
        }
        return self.manager._build_handler(
            {"name": name, "parameters": parameters, "behavior": behavior}
        )

    def test_ambiguous_commit_replay_returns_receipt_and_one_effect(self):
        self.manager.fault_flags["timeout"] = True
        handler = self.handler(
            error_when_flag_set="timeout",
            fail_before_write=False,
            clear_flag_after_first_error=True,
            return_payload={"status": "ok", "receipt_id": "R7"},
        )
        args = {"request_id": "one", "amount": 25}
        self.assertEqual("error", json.loads(handler(args))["status"])
        replay = json.loads(handler(json.dumps(args)))
        self.assertEqual("R7", replay["receipt_id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual([args], self.manager.services.snapshot()["services"]["bank"]["records"])
        self.assertEqual(1, len(self.manager.services.snapshot()["effects"]))
        self.assertFalse(self.manager.fault_flags["timeout"])

    def test_same_key_different_intent_rejected_distinct_key_accepted(self):
        handler = self.handler()
        handler({"request_id": "one", "amount": 25})
        self.assertEqual(
            "IDEMPOTENCY_CONFLICT", json.loads(handler({"request_id": "one", "amount": 30}))["code"]
        )
        handler({"request_id": "two", "amount": 25})
        self.assertEqual(2, len(self.manager.services.snapshot()["effects"]))

    def test_generated_receipt_survives_lost_ack_and_later_payments(self):
        self.manager.fault_flags["lost_ack"] = True
        pay = self.handler(
            receipt_prefix="PAY",
            error_when_flag_set="lost_ack",
            fail_before_write=False,
            clear_flag_after_first_error=True,
            return_payload={"status": "ok"},
        )
        first = {"request_id": "a", "amount": 25}
        self.assertEqual("error", json.loads(pay(first))["status"])
        replay = json.loads(pay(first))
        self.assertEqual("PAY-000001", replay["receipt_id"])
        self.assertTrue(replay["replayed"])
        second = json.loads(pay({"request_id": "b", "amount": 25}))
        self.assertEqual("PAY-000002", second["receipt_id"])
        self.assertEqual(replay, json.loads(pay(first)))
        self.assertEqual(2, len(self.manager.services.snapshot()["effects"]))

    def test_default_replay_returns_original_response_after_state_changes(self):
        pay = self.handler()
        first = {"request_id": "a", "amount": 25}
        response = json.loads(pay(first))
        pay({"request_id": "b", "amount": 25})
        self.assertEqual({**response, "replayed": True}, json.loads(pay(first)))

    def test_invalid_or_rejected_payments_do_not_allocate_receipts(self):
        pay = self.handler(
            receipt_prefix="PAY",
            requires={"ready": True},
            initial_state={"records": [], "ready": False},
        )
        self.assertEqual("error", json.loads(pay({"request_id": "a", "amount": True}))["status"])
        args = {"request_id": "a", "amount": 25}
        self.assertEqual("PRECONDITION_FAILED", json.loads(pay(args))["code"])
        self.manager.services.patch("bank", {"ready": True})
        self.assertEqual("PAY-000001", json.loads(pay(args))["receipt_id"])

    def test_concurrent_replays_have_one_effect(self):
        handler = self.handler()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda _: handler({"request_id": "one", "amount": 25}), range(8))
            )
        self.assertTrue(all(json.loads(value)["status"] == "ok" for value in results))
        snapshot = self.manager.services.snapshot()
        self.assertEqual(8, snapshot["calls"]["pay"])
        self.assertEqual(1, len(snapshot["effects"]))

    def test_before_write_fault_and_invalid_arguments_have_no_effect(self):
        self.manager.fault_flags["offline"] = True
        handler = self.handler(error_when_flag_set="offline")
        for args in (
            {"request_id": "one", "amount": 25},
            "broken",
            None,
            {"request_id": "one", "amount": True},
        ):
            self.assertEqual("error", json.loads(handler(args))["status"])
        self.assertEqual([], self.manager.services.snapshot()["effects"])
        self.assertEqual([], self.manager.services.snapshot()["services"]["bank"]["records"])

    def test_bad_container_does_not_consume_idempotency_token(self):
        handler = self.handler(initial_state={"records": {}})
        args = {"request_id": "one", "amount": 25}
        self.assertEqual("INVALID_EXISTING_STATE", json.loads(handler(args))["code"])
        self.manager.services.patch("bank", {"records": []})
        self.assertEqual("ok", json.loads(handler(args))["status"])
        self.assertEqual(1, len(self.manager.services.snapshot()["effects"]))

    def test_scripted_failures_are_counted_without_effects(self):
        handler = self.handler(
            transient_failures=3, failure_response={"status": "error", "retryable": True}
        )
        args = {"request_id": "one", "amount": 25}
        for _ in range(3):
            self.assertTrue(json.loads(handler(args))["retryable"])
        self.assertEqual([], self.manager.services.snapshot()["effects"])
        self.assertEqual("ok", json.loads(handler(args))["status"])
        self.assertEqual(4, self.manager.services.snapshot()["calls"]["pay"])

    def test_version_conflict_preserves_external_edit_and_correct_retry(self):
        spec = {
            "name": "edit",
            "parameters": {
                "type": "object",
                "properties": {
                    "expected_version": {"type": "integer"},
                    "capacity": {"type": "integer"},
                },
                "required": ["expected_version", "capacity"],
                "additionalProperties": False,
            },
            "behavior": {
                "type": "stateful_service",
                "service": "event",
                "operation": "compare_and_swap",
                "initial_state": {"version": 1, "room": "Cedar", "capacity": 100},
                "updates": {"capacity": {"$arg": "capacity"}},
            },
        }
        edit = self.manager._build_handler(spec)
        apply_perturbations(
            [
                {
                    "type": "set_mock_state",
                    "service": "event",
                    "values": {"version": 2, "room": "Maple"},
                }
            ],
            self.root,
            {},
            [],
            mock_services=self.manager.services,
        )
        self.assertEqual(
            "VERSION_CONFLICT", json.loads(edit({"expected_version": 1, "capacity": 80}))["code"]
        )
        self.assertEqual([], self.manager.services.snapshot()["effects"])
        edit({"expected_version": 2, "capacity": 80})
        self.assertEqual(
            {"version": 3, "room": "Maple", "capacity": 80},
            self.manager.services.snapshot()["services"]["event"],
        )

    def test_preconditions_and_templates_are_checked_before_mutation(self):
        handler = self.handler(
            operation="transition",
            initial_state={"ready": False},
            requires={"ready": True},
            updates={"value": {"$arg": "missing"}},
        )
        args = {"request_id": "one", "amount": 25}
        self.assertEqual("PRECONDITION_FAILED", json.loads(handler(args))["code"])
        self.manager.services.patch("bank", {"ready": True})
        self.assertIn("Missing argument", json.loads(handler(args))["error"])
        self.assertEqual([], self.manager.services.snapshot()["effects"])
        self.assertEqual({"ready": True}, self.manager.services.snapshot()["services"]["bank"])

    def test_snapshot_is_detached_and_new_manager_is_empty(self):
        handler = self.handler()
        handler({"request_id": "one", "amount": 25})
        snapshot = self.manager.services.snapshot()
        snapshot["services"]["bank"]["records"].clear()
        snapshot["effects"].clear()
        self.assertEqual(1, len(self.manager.services.snapshot()["effects"]))
        self.assertEqual({}, self.make_mock_manager(self.root, {}).services.snapshot()["services"])
        with self.assertRaisesRegex(ValueError, "Unknown mock service"):
            self.manager.services.patch("absent", {})

    def test_explicit_initial_state_is_independent_of_tool_order(self):
        spec = {
            "name": "read",
            "description": "read",
            "parameters": {"type": "object"},
            "behavior": {"type": "stateful_service", "service": "shared", "operation": "read"},
        }
        second = copy.deepcopy(spec)
        second["name"] = "other"
        second["behavior"]["initial_state"] = {"value": 7}
        try:
            self.manager.register_scenario_mocks([spec, second])
            self.assertEqual({"value": 7}, json.loads(self.registry._tools["read"]["handler"]({})))
        finally:
            self.manager.cleanup()

    def test_lookup_does_not_commit_an_effect(self):
        read = self.manager._build_handler(
            {
                "name": "page",
                "parameters": {
                    "type": "object",
                    "properties": {"cursor": {"type": "string"}},
                    "required": ["cursor"],
                },
                "behavior": {
                    "type": "stateful_service",
                    "service": "directory",
                    "operation": "lookup",
                    "lookup_arg": "cursor",
                    "responses": {"start": {"items": [1], "next_cursor": None}},
                },
            }
        )
        self.assertEqual([1], json.loads(read({"cursor": "start"}))["items"])
        self.assertEqual("NOT_FOUND", json.loads(read({"cursor": "absent"}))["code"])
        self.assertEqual([], self.manager.services.snapshot()["effects"])
