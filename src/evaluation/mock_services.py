"""Small in-process services with state independent of agent-writable task files.

These simulate application contracts, not a security boundary or a distributed DB.
Calls are serialized so tests can distinguish stale versions and duplicate effects
without relying on OS scheduling. Existing deliberately racy file mocks are separate.
"""

import copy
import json
import threading
from typing import Any

from .utils import get_json_path


def json_equal(left: Any, right: Any) -> bool:
    """JSON equality that never treats true as 1, including nested containers."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(json_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right))
    return (
        type(left) is type(right)
        and left == right
        or (type(left) in (int, float) and type(right) in (int, float) and left == right)
    )


def json_array_equal_unordered(actual, expected):
    """Typed multiset equality, retaining duplicate counts."""
    if (
        not isinstance(actual, list)
        or not isinstance(expected, list)
        or len(actual) != len(expected)
    ):
        return False
    remaining = list(actual)
    for value in expected:
        index = next((i for i, item in enumerate(remaining) if json_equal(item, value)), None)
        if index is None:
            return False
        remaining.pop(index)
    return True


class MockServices:
    def __init__(self):
        self.states = {}
        self.calls = {}
        self.effects = []
        self._tokens = {}
        self._receipt_counters = {}
        self._lock = threading.RLock()

    def register(self, behavior):
        with self._lock:
            service = behavior["service"]
            if service not in self.states:
                self.states[service] = copy.deepcopy(behavior.get("initial_state", {}))

    def patch(self, service, values):
        with self._lock:
            if service not in self.states:
                raise ValueError(f"Unknown mock service: {service}")
            self.states[service].update(copy.deepcopy(values))

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(
                {
                    "services": self.states,
                    "calls": self.calls,
                    "effects": self.effects,
                    "receipts": [
                        {"service": k[0], "tool": k[1], **v} for k, v in self._tokens.items()
                    ],
                }
            )

    def checkpoint(self):
        """Private branch state, including replay identities and receipt allocation."""
        with self._lock:
            return copy.deepcopy(
                {
                    "states": self.states,
                    "calls": self.calls,
                    "effects": self.effects,
                    "tokens": [[list(key), value] for key, value in self._tokens.items()],
                    "receipts": [
                        [list(key), value] for key, value in self._receipt_counters.items()
                    ],
                }
            )

    def restore(self, checkpoint):
        with self._lock:
            if set(checkpoint["states"]) != set(self.states):
                raise ValueError("Checkpoint services do not match registered tools")
            self.states = copy.deepcopy(checkpoint["states"])
            self.calls = copy.deepcopy(checkpoint["calls"])
            self.effects = copy.deepcopy(checkpoint["effects"])
            self._tokens = {tuple(k): copy.deepcopy(v) for k, v in checkpoint["tokens"]}
            self._receipt_counters = {tuple(k): v for k, v in checkpoint["receipts"]}

    @staticmethod
    def _template(value, args):
        if isinstance(value, dict):
            if set(value) == {"$arg"}:
                found, result = get_json_path(args, value["$arg"])
                if not found:
                    raise ValueError(f"Missing argument: {value['$arg']}")
                return copy.deepcopy(result)
            return {k: MockServices._template(v, args) for k, v in value.items()}
        if isinstance(value, list):
            return [MockServices._template(v, args) for v in value]
        return copy.deepcopy(value)

    def call(self, name, behavior, args, fault_response, parameters):
        # Validate the actual dispatched call: JSON schema in the tool catalog alone
        # does not prevent a runtime from dispatching invalid arguments.
        import jsonschema

        with self._lock:
            self.calls[name] = self.calls.get(name, 0) + 1
            try:
                jsonschema.Draft7Validator(parameters).validate(args)
            except jsonschema.ValidationError as exc:
                return json.dumps(
                    {"status": "error", "code": "INVALID_ARGUMENTS", "error": exc.message}
                )
            state = self.states[behavior["service"]]
            operation = behavior["operation"]
            if operation == "idempotent_append":
                token = args[behavior["idempotency_field"]]
                token_key = (behavior["service"], name, token)
                previous = self._tokens.get(token_key)
                if previous is not None:
                    if not json_equal(previous["arguments"], args):
                        return json.dumps({"status": "error", "code": "IDEMPOTENCY_CONFLICT"})
                    result = copy.deepcopy(previous["response"])
                    result["replayed"] = True
                    return json.dumps(result)
            for source, requirements in ((state, "requires"), (args, "requires_arguments")):
                for key, expected in behavior.get(requirements, {}).items():
                    found, value = get_json_path(source, key)
                    if not found or not json_equal(value, expected):
                        return json.dumps(
                            behavior.get(
                                "rejection", {"status": "error", "code": "PRECONDITION_FAILED"}
                            )
                        )
            if self.calls[name] <= behavior.get("transient_failures", 0):
                return json.dumps(behavior["failure_response"])
            fail_before = behavior.get("fail_before_write", True)
            if fail_before:
                error = fault_response(behavior, clear=True)
                if error is not None:
                    return error
            if operation == "read":
                return json.dumps(copy.deepcopy(state))
            if operation == "lookup":
                key = str(args[behavior["lookup_arg"]])
                return json.dumps(
                    behavior["responses"].get(key, {"status": "error", "code": "NOT_FOUND"})
                )
            try:
                updates = self._template(behavior.get("updates", {}), args)
                response = (
                    self._template(behavior["return_payload"], args)
                    if "return_payload" in behavior
                    else None
                )
            except ValueError as exc:
                return json.dumps({"status": "error", "error": str(exc)})
            if operation == "compare_and_swap":
                if type(state.get("version")) is not int:
                    return json.dumps({"status": "error", "code": "INVALID_EXISTING_STATE"})
                if not json_equal(args.get("expected_version"), state.get("version")):
                    return json.dumps(
                        {"status": "error", "code": "VERSION_CONFLICT", "current": state}
                    )
                updates["version"] = state["version"] + 1
            if operation in {"append", "idempotent_append"}:
                if not isinstance(state.get("records", []), list):
                    return json.dumps({"status": "error", "code": "INVALID_EXISTING_STATE"})
                state.setdefault("records", []).append(copy.deepcopy(args))
            state.update(updates)
            result = copy.deepcopy(
                response if response is not None else {"status": "ok", "state": state}
            )
            if operation == "idempotent_append":
                if "receipt_prefix" in behavior:
                    receipt_scope = (behavior["service"], name)
                    sequence = self._receipt_counters.get(receipt_scope, 0) + 1
                    self._receipt_counters[receipt_scope] = sequence
                    result["receipt_id"] = f"{behavior['receipt_prefix']}-{sequence:06d}"
                # Commit the original response with the operation, before a lost-ack fault.
                self._tokens[token_key] = {"arguments": copy.deepcopy(args), "response": result}
            self.effects.append({"tool": name, "arguments": copy.deepcopy(args)})
            if not fail_before:
                error = fault_response(behavior, clear=True)
                if error is not None:
                    return error
            return json.dumps(result)
