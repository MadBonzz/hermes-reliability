"""Conservative, read-only attribution from independent evaluation evidence.

Findings describe observed events, not exclusive root causes or model cognition.
No model inference, live tool replay, score changes, or historical file writes.
"""

import json

import jsonschema

from .audit_api_evidence import reconstruct_responses
from .control_observer import control_coverage
from .mock_services import json_equal
from .scenario_validation import external_schema_ref
from .utils import unwrap_tool_call


def decode(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return None


def attribute_turn(record, spec, contract, api_events, trace_events, evidence_path):
    findings = []
    number = record["turn_number"]
    origin = record.get("origin", "model")

    def add(layer, kind, claim, refs=(), certainty="observed", **extra):
        findings.append(
            {
                "layer": layer,
                "kind": kind,
                "certainty": certainty,
                "claim": claim,
                "evidence": list(refs),
                **extra,
            }
        )

    requests = {
        e["request_id"]: e
        for e in api_events
        if e["event"] == "api_request"
        and e.get("turn_number") == number
        and e.get("phase") == "agent"
    }
    response_events = [e for e in api_events if e.get("request_id") in requests]
    try:
        responses = reconstruct_responses(response_events)
    except (ValueError, TypeError, AttributeError):
        responses = {}
        add(
            "evidence",
            "response_reconstruction_failed",
            "The response stream could not be reconstructed; model-output attribution is unavailable.",
            [{"file": evidence_path + "/api.jsonl"}],
            certainty="unresolved",
        )
    executions = record.get("execution_events", [])
    wire_calls = []
    for rid, response in responses.items():
        request = requests[rid]
        schemas = {
            t.get("function", {}).get("name"): t.get("function", {}).get("parameters")
            for t in request.get("body", {}).get("tools", [])
        }
        for choice in response["choices"].values():
            for call in choice["tool_calls"].values():
                name, args = unwrap_tool_call(call["name"], call["arguments"])
                args = decode(args)
                ref = {
                    "file": evidence_path + "/api.jsonl",
                    "request_id": rid,
                    "call_id": call["id"],
                }
                wire_calls.append({"name": name, "arguments": args, "id": call["id"], "ref": ref})
                # Only blame the served output against a schema actually sent in
                # this request. Deferred schema visibility needs separate analysis.
                schema = schemas.get(name)
                if schema is None or external_schema_ref(schema):
                    continue
                try:
                    jsonschema.Draft7Validator.check_schema(schema)
                    validator = jsonschema.Draft7Validator(schema)
                    violations = list(validator.iter_errors(args))
                except Exception:
                    add(
                        "evidence",
                        "schema_validation_unavailable",
                        "Could not validate the advertised schema offline.",
                        [ref],
                        certainty="unresolved",
                    )
                    continue
                if violations:
                    execution = next((e for e in executions if e.get("id") == call["id"]), None)
                    disposition = "no_matching_execution_evidence"
                    if execution is not None:
                        parsed = decode(execution.get("result"))
                        if isinstance(parsed, dict) and (
                            parsed.get("error") or parsed.get("status") == "error"
                        ):
                            disposition = "error_returned_by_tool"
                        elif execution.get("arguments") != args:
                            disposition = "arguments_changed_before_execution"
                        else:
                            disposition = "execution_recorded_outcome_requires_review"
                    add(
                        "served_model_output",
                        "tool_contract_violation",
                        f"Provider output for {name} violates the parameter schema sent to it.",
                        [ref],
                        violations=[
                            {"path": list(v.absolute_path), "rule": v.validator} for v in violations
                        ],
                        disposition=disposition,
                        limitation="Does not distinguish model generation from provider-side serialization, or prove this caused the final failure.",
                    )

    for event in response_events:
        ref = {"file": evidence_path + "/api.jsonl", "request_id": event["request_id"]}
        if event["event"] == "api_response_headers" and event["status_code"] >= 400:
            add(
                "provider_or_configuration",
                "http_rejection",
                f"Provider returned HTTP {event['status_code']}.",
                [ref],
                limitation="A rejection is not proof of a provider defect; request configuration, quota or capacity can cause it.",
            )
        elif event["event"] == "api_request_error":
            add("transport", "request_exception", "HTTP dispatch raised an exception.", [ref])

    mocks = {m["name"]: m for m in (contract or {}).get("mock_tools", [])}
    for call in executions:
        result = decode(call.get("result"))
        # Hermes can append loop advice after a JSON result; retain raw evidence
        # and avoid inferring a successful tool just because full JSON parsing fails.
        if result is None and isinstance(call.get("result"), str):
            try:
                result, _ = json.JSONDecoder().raw_decode(call["result"].lstrip())
            except ValueError:
                pass
        if not isinstance(result, dict):
            continue
        ref = {"file": evidence_path + f"/turn_{number}.jsonl", "call_id": call.get("id")}
        error = result.get("error") or result.get("status") == "error"
        if result.get("code") == "EVAL_PATH_DENIED":
            add(
                "evaluation_harness",
                "path_guard_rejection",
                "The harness path guard rejected this operation.",
                [ref],
            )
        elif result.get("stale_write_blocked"):
            add(
                "hermes_runtime",
                "overwrite_guard_rejection",
                "Hermes's overwrite guard rejected this operation.",
                [ref],
                limitation="Refusal correctness is unresolved; a complete-read metadata defect can cause a false refusal.",
            )
        elif call["name"] in mocks and error:
            add(
                "mock_service",
                "service_rejection_or_injected_fault",
                "The evaluator-owned service returned an error.",
                [ref],
                limitation="Consult the mock definition and before/after effects to distinguish a precondition rejection from an injected fault or an error after commit.",
            )
        elif error:
            add(
                "runtime_tool",
                "tool_error_returned",
                f"{call['name']} returned an error to the agent.",
                [ref],
                limitation="An error response alone is not a runtime bug; invalid arguments, permissions or missing inputs can be legitimate causes.",
            )

    # A narrow independent consistency check: an executed required call disproves
    # a saved 'not called' verdict. Do not compare aggregate pass/fail blindly.
    process_specs = (spec or {}).get("process_checks", [])
    saved = record.get("process_check", {}).get("details", [])
    for index, check in enumerate(process_specs):
        if index >= len(saved) or not saved[index].startswith("[FAIL]"):
            continue
        if check["type"] in {"tool_called", "any_tool_called"}:
            targets = {check["tool_name"]} if "tool_name" in check else set(check["tool_names"])
            matching = [c for c in executions if c["name"] in targets and c.get("completed")]
            if matching:
                add(
                    "verifier",
                    "required_call_false_negative",
                    "A required tool was recorded as executed, but the process assertion says it was not called.",
                    [
                        {"file": evidence_path + f"/turn_{number}.jsonl", "call_id": c.get("id")}
                        for c in matching
                    ],
                    check_index=index,
                    contribution="This failed assertion directly closed the scenario gate.",
                )

    # Identify duplicate retries only when the oracle explicitly permits exactly
    # one committed effect of this tool. Repeated identical business requests can
    # otherwise be legitimate (including the next turn in scenario 47).
    effects = record.get("mock_snapshot", {}).get("effects", [])
    before = next(
        (
            e.get("mock_snapshot", {}).get("effects", [])
            for e in trace_events
            if e["event"] == "before_agent"
        ),
        None,
    )
    for name, mock in mocks.items():
        behavior = mock["behavior"]
        if behavior.get("operation") != "idempotent_append":
            continue
        token = behavior["idempotency_field"]
        targets = [c for c in wire_calls if c["name"] == name and isinstance(c["arguments"], dict)]
        cap = any(
            c["type"] == "mock_effects_equal" and c.get("expected", []).count(name) == 1
            for c in (spec or {}).get("state_checks", [])
        )
        if not cap:
            continue
        for index, call in enumerate(targets):
            args = call["arguments"]
            earlier = next(
                (
                    c
                    for c in targets[:index]
                    if token in args
                    and token in c["arguments"]
                    and json_equal(
                        {k: v for k, v in c["arguments"].items() if k != token},
                        {k: v for k, v in args.items() if k != token},
                    )
                    and c["arguments"].get(token) != args.get(token)
                ),
                None,
            )
            if earlier is None:
                continue
            add(
                "served_model_output",
                "retry_identity_changed",
                "The served output used different idempotency tokens for identical business parameters within a one-effect task.",
                [earlier["ref"], call["ref"]],
                limitation="The task permits one effect; the service deduplicates tokens, not semantic business intent.",
            )
            if before is not None and sum(e["tool"] == name for e in effects[len(before) :]) > 1:
                add(
                    "application_workflow",
                    "application_duplicate_effects",
                    "More than one effect committed in a task whose oracle permits one. This is an application-workflow violation, not a demonstrated failure of a Hermes-enforceable contract.",
                    [
                        call["ref"],
                        {
                            "file": evidence_path + f"/turn_{number}.jsonl",
                            "event": "turn_end.mock_snapshot.effects",
                        },
                    ],
                    native_hermes_bug="not_applicable_without_explicit_runtime_contract",
                    limitation="The mock's token deduplication contract was not violated. Native Hermes business-level deduplication is not established by this test.",
                )
            break

    if record.get("harness_errors"):
        add(
            "evaluation_harness",
            "reported_harness_error",
            "The runner recorded a harness or cleanup error.",
            [{"file": evidence_path + f"/turn_{number}.jsonl", "event": "turn_end.harness_errors"}],
        )
    if record.get("agent_failure_kind") == "iteration_limit":
        add(
            "agent_system",
            "iteration_limit",
            "The turn exhausted its configured iteration budget.",
            limitation="Budget exhaustion does not by itself locate the cause in model, runtime or task complexity.",
        )
    elif record.get("agent_error") and not any(
        f["layer"] in {"transport", "provider_or_configuration"} for f in findings
    ):
        add(
            "agent_system",
            "runtime_error_not_localized",
            "The agent reported an error; the available rules do not identify its cause.",
            certainty="unresolved",
        )
    if origin == "authored_reference":
        # Authored prefixes are evaluator work, never positive/negative model evidence.
        for finding in findings:
            finding["execution_origin"] = "authored_reference"
    return {
        "turn_number": number,
        "origin": origin,
        "task_gate_passed": record["gate_passed"],
        "challenge_exposure": "turn_executed_challenge_exposure_requires_review",
        "property_verdict": "observed_duplicate_violation"
        if any(f["kind"] == "application_duplicate_effects" for f in findings)
        else "not_automatically_attributed",
        "findings": findings,
        "native_control_coverage": control_coverage(executions, trace_events),
        "native_control_instrumentation": next(
            (
                e.get("native_control_observation", {"mode": "not_recorded"})
                for e in trace_events
                if e["event"] == "turn_start"
            ),
            {"mode": "not_recorded"},
        ),
        "unresolved": "No listed finding means no supported diagnosis, not that the model/runtime/verifier is correct. Passing checks do not prove native enforcement.",
    }
