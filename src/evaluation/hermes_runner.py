"""Hermes turn execution, mock lifecycle, evidence capture and assertion gates."""

import copy
import re
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from . import utils
from .api_trace import APITrace, SecretRedactor
from .checks import (
    evaluate_output_checks,
    evaluate_process_checks,
    evaluate_state_checks,
)
from .control_observer import NativeControlObserver, control_coverage
from .execution_isolation import file_tool_guard
from .mock_registry import DynamicMockToolManager
from .perturbations import apply_perturbations, force_unlock_workspace
from .reference_execution import ReferenceAgent, causal_observations
from .runtime_support import (
    TurnTrace,
    current_turn_messages,
    json_safe,
    utc_now,
    workspace_manifest,
)
from .utils import (
    check_tool_catalog,
    extract_turn_tool_calls,
    hermes_workspace,
    instantiate_hermes_agent,
    resolve_ws_path,
)


def classify_agent_result(result, max_iterations):
    """Separate a bounded unfinished task from a runtime/infrastructure failure."""
    incomplete = (
        result.get("error")
        or result.get("failed") is True
        or result.get("partial") is True
        or result.get("interrupted") is True
        or result.get("completed") is False
        or result.get("cleanup_errors")
    )
    if not incomplete:
        return None, None
    reason = result.get("turn_exit_reason")
    calls = result.get("api_calls")
    # Hermes can label a tool tail at the iteration cap as pending_tool_result
    # and stamp loop_error. An empty last response can instead leave the exit
    # reason unknown, completed=False and all failure flags clear. Only accept
    # that variant when no reported error contradicts exhaustion at the cap.
    unknown_at_cap = (
        reason == "unknown"
        and result.get("completed") is False
        and result.get("failed") is False
        and result.get("partial") is False
        and not result.get("error")
        and not result.get("failure_reason")
    )
    bounded_exit = (
        reason in {"budget_exhausted", "pending_tool_result"}
        or (isinstance(reason, str) and reason.startswith("max_iterations_reached("))
        or unknown_at_cap
    )
    exhausted = (
        bounded_exit
        and type(calls) is int
        and calls >= max_iterations
        and result.get("failure_reason") in (None, "loop_error")
        and not result.get("interrupted")
        and not result.get("cleanup_errors")
    )
    error = str(result.get("error") or f"Hermes turn did not complete: {reason or 'unknown'}")
    return error, "iteration_limit" if exhausted else "runtime_error"


def _run_turn(
    turn,
    *,
    agent,
    history,
    workspace,
    manager,
    report,
    trace_root,
    checkpoint,
    max_iterations,
    api_trace=None,
    control_observer=None,
):
    """Execute and checkpoint one turn, always applying its post-turn cleanup."""
    run_id, sid, task_id = report["run_id"], report["id"], report["task_id"]
    number = turn["turn_number"]
    prompt = turn["prompt"].replace("{WORKSPACE}", workspace.as_posix())
    sent_prompt = prompt
    trace_path = Path(trace_root) / sid / f"turn_{number}.jsonl" if trace_root else None
    trace = TurnTrace(trace_path, redact=api_trace.redact if api_trace else None)
    record = {
        "turn_number": number,
        "prompt": prompt,
        "sent_prompt": sent_prompt,
        "started_at_utc": utc_now(),
        "trace_path": str(trace_path) if trace_path else None,
        "gate_passed": False,
        "harness_errors": [],
        "agent_error": None,
        "agent_failure_kind": None,
        "perturbations": [],
        "cleanup": [],
        "tool_calls": [],
        "final_answer": "",
    }
    report["turns"].append(record)

    def tool_start(call_id, name, arguments, *unused, **kwargs):
        if control_observer:
            control_observer.start_call(trace.record, call_id, name)
        trace.tool_start(call_id, name, arguments)
        trace.record(
            "tool_state_before",
            id=call_id,
            workspace=workspace_manifest(workspace),
            mock_snapshot=manager.services.snapshot(),
            fault_flags=copy.deepcopy(manager.fault_flags),
        )

    def tool_complete(call_id, name, arguments, result, *unused, **kwargs):
        if control_observer:
            control_observer.end_call(call_id)
        trace.tool_complete(call_id, name, arguments, result)
        trace.record(
            "tool_state_after",
            id=call_id,
            workspace=workspace_manifest(workspace),
            mock_snapshot=manager.services.snapshot(),
            fault_flags=copy.deepcopy(manager.fault_flags),
        )

    agent.tool_start_callback = tool_start
    agent.tool_complete_callback = tool_complete
    trace.record(
        "turn_start",
        run_id=run_id,
        scenario_id=sid,
        turn_number=number,
        prompt=sent_prompt,
        input_history=history,
        tool_catalog=check_tool_catalog(agent, manager.registered_schemas),
        execution_kind=report.get("execution_kind", "ordinary"),
        origin="authored_reference" if number <= report.get("reference_until", 0) else "model",
        native_control_observation=control_observer.manifest
        if control_observer
        else {"mode": "not_enabled"},
    )
    start = time.monotonic()
    try:
        if checkpoint:
            checkpoint(report)
        if api_trace:
            api_trace.set_context(number, "pre_turn_perturbations")
        apply_perturbations(
            turn.get("pre_turn_perturbations", []),
            workspace,
            manager.fault_flags,
            history,
            agent,
            record["perturbations"],
            mock_services=manager.services,
        )
        record["workspace_before_agent"] = workspace_manifest(workspace)
        trace.record(
            "before_agent",
            history=history,
            perturbations=record["perturbations"],
            workspace=record["workspace_before_agent"],
            mock_snapshot=manager.services.snapshot(),
            fault_flags=copy.deepcopy(manager.fault_flags),
        )
        if api_trace:
            api_trace.set_context(number, "agent")
        try:
            result = agent.run_conversation(
                user_message=sent_prompt, conversation_history=history, task_id=task_id
            )
            result = json_safe(result)
            if not isinstance(result, dict):
                raise TypeError("Hermes returned a non-object result")
            record["agent_termination"] = {
                key: result[key]
                for key in (
                    "completed",
                    "failed",
                    "partial",
                    "interrupted",
                    "turn_exit_reason",
                    "failure_reason",
                    "api_calls",
                    "cleanup_errors",
                )
                if key in result
            }
            record["agent_error"], record["agent_failure_kind"] = classify_agent_result(
                result, max_iterations
            )
        except Exception as exc:
            record["agent_error"] = f"{type(exc).__name__}: {exc}"
            record["agent_failure_kind"] = "runtime_error"
            result = {"final_response": "", "messages": history}
        trace.record("agent_result", result=result, error=record["agent_error"])
        history = result.get("messages", history)
        record["final_answer"] = result.get("final_response", "") or ""
        messages = current_turn_messages(history, sent_prompt)
        emitted = extract_turn_tool_calls(messages, 0) if messages is not None else None
        executed = trace.tool_calls()
        record["execution_events"] = executed
        if emitted is not None:
            record["tool_calls"] = emitted
            record["process_trace_source"] = "assistant_messages"
        elif executed:
            record["tool_calls"] = executed
            record["process_trace_source"] = "execution_callbacks_after_history_rewrite"
        elif record["agent_error"] is None:
            raise RuntimeError(
                "Cannot identify current turn in returned history; refusing to score an empty trace"
            )
        record["mock_snapshot"] = manager.services.snapshot()
        for key, evaluator, value in (
            ("state", evaluate_state_checks, workspace),
            ("process", evaluate_process_checks, record["tool_calls"]),
            ("output", evaluate_output_checks, record["final_answer"]),
        ):
            options = (
                {
                    "mock_snapshot": record["mock_snapshot"],
                    "before_workspace": record["workspace_before_agent"],
                }
                if key == "state"
                else {}
            )
            ok, details = evaluator(turn.get(key + "_checks", []), value, **options)
            record[key + "_check"] = {"passed": ok, "details": details}
        record["workspace_after_agent"] = workspace_manifest(workspace)
    except Exception as exc:
        record["harness_errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        # Preserve external-service evidence even when history extraction/checking fails.
        record.setdefault("mock_snapshot", manager.services.snapshot())
        try:
            if api_trace:
                api_trace.set_context(number, "post_turn_cleanup")
            apply_perturbations(
                turn.get("post_turn_cleanup", []),
                workspace,
                manager.fault_flags,
                history,
                agent,
                record["cleanup"],
                mock_services=manager.services,
            )
        except Exception as exc:
            record["harness_errors"].append(f"Cleanup: {type(exc).__name__}: {exc}")
        if api_trace:
            try:
                api_trace.raise_if_failed()
            except RuntimeError as exc:
                record["harness_errors"].append(str(exc))
            record["api_trace_path"] = str(api_trace.path)
        record["elapsed_sec"] = round(time.monotonic() - start, 2)
        record["finished_at_utc"] = utc_now()
        record["gate_passed"] = (
            not record["harness_errors"]
            and record["agent_error"] is None
            and all(
                record.get(k + "_check", {}).get("passed", False)
                for k in ("state", "process", "output")
            )
        )
        record["causal_evidence"] = causal_observations(record, manager.registered_schemas)
        if control_observer:
            record["native_control_events"] = [
                e for e in trace.events if e["event"].startswith("native_control_")
            ]
            record["control_coverage"] = control_coverage(
                record.get("execution_events", []), trace.events
            )
            if control_observer.errors:
                record["harness_errors"].extend(control_observer.errors)
                record["gate_passed"] = False
                record["causal_evidence"]["task_gate_passed"] = False
        trace.record("turn_end", record=record)
        if checkpoint:
            checkpoint(report)
    return history, record


def run_single_scenario(
    scenario,
    base_url,
    model_name,
    api_key,
    workspace_root,
    max_iterations=15,
    run_id=None,
    trace_root=None,
    checkpoint=None,
    redactor=None,
    guard_file_paths=False,
    reference_until=0,
    request_pacer=None,
    observe_native_controls=False,
    workspace_name=None,
    initial_history=None,
    initial_services=None,
    initial_fault_flags=None,
    stop_before_turn=None,
    input_validator=None,
):
    run_id = run_id or uuid.uuid4().hex
    sid = scenario["id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        raise ValueError(f"Unsafe scenario ID: {sid!r}")
    workspace = resolve_ws_path(workspace_root, workspace_name or sid)
    report = {
        "id": sid,
        "name": scenario["name"],
        "property_tested": scenario.get("property_tested", ""),
        # Freeze the private evaluator contract for later attribution. This is
        # never injected into the model's prompt or tool catalog.
        "scenario_contract": copy.deepcopy(scenario),
        "run_id": run_id,
        "workspace": str(workspace),
        "status": "running",
        "started_at_utc": utc_now(),
        "passed_all_turns": False,
        "failed_at_turn": None,
        "turns": [],
        "harness_errors": [],
        "execution_kind": "reference_continuation" if reference_until else "ordinary",
        "reference_until": reference_until,
        "reference_prefix": [],
        "neutral_presentation": True,
    }
    manager = DynamicMockToolManager(
        workspace,
        {},
        registry=utils.registry,
        toolsets=utils.toolsets,
        model_tools=utils.model_tools,
    )
    workspace_scope = ExitStack()
    task_id = uuid.uuid4().hex
    report["task_id"] = task_id
    agent = None
    api_trace = None
    workspace_created = False
    try:
        # Refuse to reuse or destroy a previous run's artifacts.
        workspace.mkdir(parents=True, exist_ok=False)
        workspace_created = True
        workspace_scope.enter_context(hermes_workspace(workspace, task_id))
        if trace_root is not None:
            api_trace = workspace_scope.enter_context(
                APITrace(
                    Path(trace_root) / sid / "api.jsonl",
                    run_id=run_id,
                    scenario_id=sid,
                    redactor=redactor or SecretRedactor([api_key]),
                    pacer=request_pacer,
                    input_validator=input_validator,
                )
            )
            report["api_trace_path"] = str(api_trace.path)
        manager.register_scenario_mocks(scenario.get("mock_tools", []))
        control_observer = None
        if observe_native_controls:
            control_observer = workspace_scope.enter_context(NativeControlObserver())
            report["native_control_observation"] = control_observer.manifest
        if guard_file_paths:
            report["file_access_policy"] = workspace_scope.enter_context(
                file_tool_guard(
                    utils.registry,
                    workspace,
                    utils.tool_working_directory,
                    neutral_messages=True,
                )
            )
        agent = instantiate_hermes_agent(base_url, model_name, api_key, max_iterations, workspace)
        manager.attach_to_agent_instance(agent)
        report["reasoning_config"] = json_safe(getattr(agent, "reasoning_config", None))
        report["tool_catalog"] = check_tool_catalog(agent, manager.registered_schemas)
        history = copy.deepcopy(initial_history or [])
        if initial_services is not None:
            manager.services.restore(initial_services)
        manager.fault_flags.update(copy.deepcopy(initial_fault_flags or {}))
        report["passed_all_turns"] = True
        for turn in scenario["turns"]:
            if stop_before_turn is not None and turn["turn_number"] >= stop_before_turn:
                report["status"] = "prefix_ready"
                break
            is_reference = turn["turn_number"] <= reference_until
            acting_agent = agent
            if is_reference:
                if "reference" not in turn:
                    raise ValueError("Missing authored reference prefix")
                acting_agent = ReferenceAgent(agent, turn["reference"], workspace)
            history, record = _run_turn(
                turn,
                agent=acting_agent,
                history=history,
                workspace=workspace,
                manager=manager,
                report=report,
                trace_root=trace_root,
                checkpoint=None if is_reference else checkpoint,
                max_iterations=max_iterations,
                api_trace=api_trace,
                control_observer=control_observer,
            )
            record["origin"] = "authored_reference" if is_reference else "model"
            if is_reference:
                report["turns"].remove(record)
                report["reference_prefix"].append(record)
                record["reference_history"] = copy.deepcopy(history)
                if not record["gate_passed"]:
                    raise RuntimeError(
                        f"Reference prefix failed validation at turn {turn['turn_number']}"
                    )
                record["expected_correct_state"] = workspace_manifest(workspace)
                continue
            number = turn["turn_number"]
            print(
                f"  {sid} turn {number}: {'PASS' if record['gate_passed'] else 'FAIL'} ({len(record['tool_calls'])} calls)"
                + (f" [{record['agent_failure_kind']}]" if record["agent_failure_kind"] else ""),
                flush=True,
            )
            if not record["gate_passed"]:
                report["passed_all_turns"] = False
                report["failed_at_turn"] = number
                report["status"] = (
                    "error"
                    if record["harness_errors"] or record["agent_failure_kind"] == "runtime_error"
                    else "failed"
                )
                break
        else:
            report["status"] = "passed"
        report["continuation"] = {
            "history": copy.deepcopy(history),
            "services": manager.services.checkpoint(),
            "fault_flags": copy.deepcopy(manager.fault_flags),
        }
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        report["passed_all_turns"] = False
        raise
    except Exception as exc:
        report["harness_errors"].append(f"{type(exc).__name__}: {exc}")
        report["status"] = "error"
        report["passed_all_turns"] = False
    finally:
        if api_trace:
            api_trace.set_context(None, "scenario_cleanup")
        # Each cleanup runs even if the other cleanup operations fail.
        cleanups = [("workspace binding", workspace_scope.close), ("registry", manager.cleanup)]
        if agent is not None and callable(getattr(agent, "close", None)):
            cleanups.insert(0, ("agent", agent.close))
        if workspace_created:
            cleanups.append(("permissions", lambda: force_unlock_workspace(workspace)))
        for label, cleanup in cleanups:
            try:
                cleanup()
            except Exception as exc:
                report["harness_errors"].append(f"{label} cleanup: {type(exc).__name__}: {exc}")
                report["passed_all_turns"] = False
                report["status"] = "error"
        report["finished_at_utc"] = utc_now()
        if api_trace:
            report["api_requests"] = api_trace.requests
        if checkpoint:
            checkpoint(report)
    return report
