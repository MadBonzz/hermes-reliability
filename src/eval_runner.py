#!/usr/bin/env python3
"""
Declarative Multi-Turn Evaluation Runner for Hermes Agent.
Ingests a standardized JSON scenario suite, dynamically registers/cleans up mock tools,
applies turn-level perturbations (including native Hermes context compression),
and enforces 3-tier (State, Process, Output) gates after every turn.
"""

import argparse
import copy
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

# Support direct script execution while keeping helpers namespaced. Hermes has
# its own top-level utils.py, so importing our helpers as `utils` would collide.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import utils
from src.api_trace import APITrace, RequestPacer, SecretRedactor
from src.checks import evaluate_output_checks, evaluate_process_checks, evaluate_state_checks
from src.control_observer import NativeControlObserver, control_coverage
from src.execution_isolation import file_tool_guard
from src.mock_registry import DynamicMockToolManager
from src.perturbations import apply_perturbations, force_unlock_workspace
from src.reference_execution import ReferenceAgent, causal_observations
from src.runtime_support import (
    TurnTrace,
    current_turn_messages,
    json_safe,
    sha256_file,
    utc_now,
    workspace_manifest,
    write_json_atomic,
)
from src.scenario_validation import DEFAULT_SUITE_NAME, load_scenarios, suite_sha256
from src.utils import (
    check_tool_catalog,
    extract_turn_tool_calls,
    hermes_run_environment,
    hermes_workspace,
    instantiate_hermes_agent,
    load_eval_config,
    load_hermes,
    resolve_ws_path,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
    marker = f"[EVAL run={run_id} scenario={sid} turn={number}]"
    sent_prompt = prompt if report.get("neutral_presentation") else marker + "\n" + prompt
    trace_path = Path(trace_root) / sid / f"turn_{number}.jsonl" if trace_root else None
    trace = TurnTrace(trace_path, redact=api_trace.redact if api_trace else None)
    record = {
        "turn_number": number,
        "prompt": prompt,
        "sent_prompt": sent_prompt,
        "correlation_marker": marker,
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
    neutral_presentation=False,
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
        "neutral_presentation": neutral_presentation,
    }
    manager = DynamicMockToolManager(
        workspace,
        {},
        registry=utils.registry,
        toolsets=utils.toolsets,
        model_tools=utils.model_tools,
    )
    workspace_scope = ExitStack()
    task_id = uuid.uuid4().hex if neutral_presentation else f"{run_id}_{sid}"
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
                    neutral_messages=neutral_presentation,
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
        if neutral_presentation:
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


def remove_run_workspace(workspace, parent, run_id):
    """Delete only this run's owned directory, refusing redirected paths."""
    expected = Path(parent).resolve() / run_id
    workspace = Path(workspace)
    if workspace.absolute() != expected or workspace.resolve() != expected:
        raise ValueError("Workspace cleanup target does not match the current run")
    if not workspace.exists():
        return
    # Permission restoration must not follow symlinks or Windows junctions.
    for directory, dirs, files in os.walk(workspace, followlinks=False):
        for path in [Path(directory), *(Path(directory) / name for name in dirs + files)]:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Refusing workspace cleanup through a link: {path}")
    force_unlock_workspace(workspace)
    shutil.rmtree(workspace)


def build_summary(suite_name, model, base_url, reports, total, **metadata):
    turns = [t for r in reports for t in r["turns"]]
    passed = sum(r["passed_all_turns"] and r["status"] == "passed" for r in reports)
    errors = sum(r["status"] == "error" for r in reports)
    return dict(
        suite_name=suite_name,
        model=model,
        base_url=base_url,
        total_scenarios=total,
        completed_scenarios=sum(r["status"] in {"passed", "failed", "error"} for r in reports),
        passed_scenarios=passed,
        harness_or_runtime_error_scenarios=errors,
        pass_rate=round(passed / max(total, 1), 4),
        attempted_turns=len(turns),
        passed_turns=sum(t["gate_passed"] for t in turns),
        iteration_limited_turns=sum(
            t.get("agent_failure_kind") == "iteration_limit" for t in turns
        ),
        tool_calls=sum(len(t["tool_calls"]) for t in turns),
        reports=reports,
        **metadata,
    )


def parse_args(argv=None, *, v2=False):
    config = load_eval_config(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(
        description="Run declarative multi-turn evaluations on Hermes Agent."
    )
    parser.add_argument(
        "--scenarios",
        default=str(PROJECT_ROOT / ("scenarios_v2" if v2 else "scenarios")),
        help="Directory of standalone scenario JSON files, or one scenario JSON file",
    )
    parser.add_argument(
        "--suite-name",
        default=DEFAULT_SUITE_NAME,
        help="Label for this evaluation in the result report",
    )
    parser.add_argument(
        "--base-url",
        default=config.get("EVAL_BASE_URL", "http://localhost:1235/v1"),
        help="Local or hosted API root, e.g. https://openrouter.ai/api/v1 (EVAL_BASE_URL)",
    )
    parser.add_argument(
        "--model",
        default=config.get("EVAL_MODEL", "nvidia/nemotron-3-nano-4b"),
        help="Exact model ID; never silently substituted",
    )
    parser.add_argument(
        "--api-key",
        default=config.get("EVAL_API_KEY", "lm-studio"),
        help="Bearer API key (EVAL_API_KEY); local servers may ignore it",
    )
    parser.add_argument(
        "--scenario-id", action="append", help="Repeat to select multiple scenarios"
    )
    if v2:
        parser.add_argument(
            "--output", help="New run directory; defaults to eval_harness/v2_runs/<run-id>"
        )
        parser.add_argument("--repetitions", type=int, default=1)
        parser.add_argument("--prices")
        parser.add_argument("--order-seed", type=int, default=0)
    else:
        parser.add_argument(
            "--output", help="Defaults to eval_harness/results/runs/<run-id>/eval_report.json"
        )
        parser.add_argument("--overwrite-output", action="store_true")
        parser.add_argument(
            "--workspace-root", default=str(PROJECT_ROOT / "eval_harness/eval_workspace")
        )
        parser.add_argument(
            "--keep-workspace",
            action="store_true",
            help="Retain this run's task files after completion, failure, or interruption",
        )
    parser.add_argument("--hermes-root", default=config.get("HERMES_AGENT_ROOT"))
    parser.add_argument("--max-iterations", type=int, default=15)
    parser.add_argument(
        "--provider", choices=["configured", "cohere", "baseten"], default="configured"
    )
    parser.add_argument(
        "--request-interval",
        type=float,
        default=None,
        help="Minimum seconds between API dispatches; Cohere defaults to 7",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate suite without importing Hermes or contacting a model server",
    )
    args = parser.parse_args(argv)
    if v2 and args.repetitions < 1:
        parser.error("--repetitions must be positive")
    if args.provider in {"cohere", "baseten"}:
        supplied = sys.argv[1:] if argv is None else argv

        def explicit(option):
            return any(v == option or v.startswith(option + "=") for v in supplied)

        if args.provider == "cohere":
            base_url = "https://api.cohere.ai/compatibility/v1"
            model = "command-a-plus-05-2026"
            key_names = ("COHERE_API_KEY", "COHERE_KEY")
        else:
            base_url = "https://inference.baseten.co/v1"
            model = "meta-models/muse-spark-open-1026"
            key_names = ("BASETEN_API_KEY", "BASETEN_KEY", "BAESTEN_KEY")
        if not explicit("--base-url"):
            args.base_url = base_url
        if not explicit("--model"):
            args.model = model
        if not explicit("--api-key"):
            args.api_key = next((config.get(k) for k in key_names if config.get(k)), None)
        if not args.api_key:
            parser.error(f"Set {' or '.join(key_names)} for --provider {args.provider}")
    if args.max_iterations <= 0:
        parser.error("--max-iterations must be positive")
    if args.request_interval is None:
        args.request_interval = 7 if args.provider == "cohere" else 0
    if not 0 <= args.request_interval <= 60:
        parser.error("--request-interval must be between 0 and 60 seconds")
    return parser, args


def main(argv=None):
    parser, args = parse_args(argv)
    scenarios_path = Path(args.scenarios).resolve()
    schema_path = PROJECT_ROOT / "scenario.schema.json"
    try:
        suite, scenario_sources = load_scenarios(scenarios_path, schema_path, args.suite_name)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    scenarios = suite["scenarios"]
    if args.scenario_id:
        missing = set(args.scenario_id) - {s["id"] for s in scenarios}
        if missing:
            parser.error(f"Unknown scenario IDs: {sorted(missing)}")
        scenarios = [s for s in scenarios if s["id"] in args.scenario_id]
    selected_ids = {s["id"] for s in scenarios}
    suite = {"suite_name": suite["suite_name"], "scenarios": scenarios}
    scenario_sources = [source for source in scenario_sources if source["id"] in selected_ids]
    if args.validate_only:
        print(
            f"Validated {len(scenarios)} scenarios, {sum(len(s['turns']) for s in scenarios)} turns"
        )
        return 0
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    run_dir = PROJECT_ROOT / "eval_harness/results/runs" / run_id
    output = Path(args.output).resolve() if args.output else run_dir / "eval_report.json"
    if output.exists() and not args.overwrite_output:
        parser.error(f"Output already exists: {output}; choose another or use --overwrite-output")
    workspace_parent = Path(args.workspace_root).resolve()
    workspace_root = workspace_parent / run_id
    if output.is_relative_to(workspace_root) or run_dir.resolve().is_relative_to(workspace_root):
        parser.error("Reports and traces must be outside the run workspace")
    workspace_created = False
    reports = []
    diagnostic_reports = []
    runtime_scope = ExitStack()
    metadata = {
        "run_id": run_id,
        "started_at_utc": utc_now(),
        "status": "running",
        "planned_turns": sum(len(s["turns"]) for s in scenarios),
        "python_version": sys.version,
        "python_executable": sys.executable,
        "scenario_files": scenario_sources,
        "suite_sha256": suite_sha256(suite),
        "schema_sha256": sha256_file(schema_path),
        "runner_sha256": sha256_file(__file__),
        "implementation_sha256": {
            p.name: sha256_file(p) for p in sorted((PROJECT_ROOT / "src").glob("*.py"))
        },
        "reference_protocol_version": 1,
        "max_iterations": args.max_iterations,
        "workspace_root": str(workspace_root),
        "trace_root": str(run_dir / "traces"),
        "keep_workspace": args.keep_workspace,
        "api_trace_format_version": 1,
        "hermes_home": str(run_dir / "hermes_home"),
        "hermes_config": utils.EVAL_HERMES_CONFIG,
        "diagnostic_reports": diagnostic_reports,
    }
    redactor = SecretRedactor([args.api_key])
    request_pacer = RequestPacer(args.request_interval)
    metadata["request_interval_seconds"] = args.request_interval

    def checkpoint(report=None):
        if report is not None and not any(r is report for r in reports):
            reports.append(report)
        write_json_atomic(
            output,
            redactor(
                build_summary(
                    suite["suite_name"],
                    args.model,
                    args.base_url,
                    reports,
                    len(scenarios),
                    **metadata,
                )
            ),
        )

    checkpoint()
    try:
        workspace_root.mkdir(parents=True, exist_ok=False)
        workspace_created = True
        runtime_scope.enter_context(hermes_run_environment(run_dir / "hermes_home"))
        root = load_hermes(args.hermes_root)
        metadata["hermes_root"] = str(root)
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
        )
        metadata["hermes_revision"] = revision.stdout.strip() if revision.returncode == 0 else None
        for scenario in scenarios:
            print(f"Starting {scenario['id']} ({len(scenario['turns'])} turns)", flush=True)
            report = run_single_scenario(
                scenario,
                args.base_url,
                args.model,
                args.api_key,
                workspace_root,
                args.max_iterations,
                run_id,
                run_dir / "traces",
                checkpoint,
                redactor=redactor,
                guard_file_paths=True,
                observe_native_controls=True,
                request_pacer=request_pacer,
            )
            if report["status"] == "error":
                # Infrastructure failure invalidates continued model comparison.
                metadata["status"] = "error"
                print(
                    f"Suite stopped on runtime/harness error in {scenario['id']}; see report for details.",
                    flush=True,
                )
                break
            failed = report.get("failed_at_turn")
            while failed and failed < len(scenario["turns"]):
                variant = copy.deepcopy(scenario)
                variant["id"] += f"__reference_after_{failed}"
                print(f"Starting diagnostic continuation {variant['id']}", flush=True)

                def diagnostic_checkpoint(value):
                    if not any(r is value for r in diagnostic_reports):
                        diagnostic_reports.append(value)
                    checkpoint()

                diagnostic = run_single_scenario(
                    variant,
                    args.base_url,
                    args.model,
                    args.api_key,
                    workspace_root,
                    args.max_iterations,
                    run_id,
                    run_dir / "traces",
                    diagnostic_checkpoint,
                    redactor=redactor,
                    guard_file_paths=True,
                    observe_native_controls=True,
                    reference_until=failed,
                    request_pacer=request_pacer,
                )
                diagnostic["source_scenario_id"] = scenario["id"]
                diagnostic["branched_after_failure_at_turn"] = failed
                if diagnostic["status"] == "error":
                    metadata["status"] = "error"
                    break
                failed = diagnostic.get("failed_at_turn")
            if metadata["status"] == "error":
                break
        else:
            metadata["status"] = "complete"
    except KeyboardInterrupt:
        metadata["status"] = "interrupted"
    except Exception as exc:
        metadata["status"] = "error"
        metadata["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        metadata["finished_at_utc"] = utc_now()
        metadata["workspace_cleanup"] = {"status": "not_created"}
        if workspace_created:
            metadata["workspace_cleanup"]["status"] = "kept" if args.keep_workspace else "pending"
        try:
            if workspace_created:
                # Save final evidence even when execution stopped before turn scoring.
                # Include misplaced files and files in unreported sibling directories.
                metadata["final_run_workspace"] = workspace_manifest(workspace_root)
                for report in reports + diagnostic_reports:
                    scenario_workspace = resolve_ws_path(workspace_root, report["id"])
                    report["final_workspace"] = workspace_manifest(scenario_workspace)
            checkpoint()  # Never discard task files if evidence cannot be saved.
            if workspace_created and not args.keep_workspace:
                remove_run_workspace(workspace_root, workspace_parent, run_id)
                metadata["workspace_cleanup"]["status"] = "deleted"
                checkpoint()
        except Exception as exc:
            metadata["status"] = "error"
            metadata["workspace_cleanup"] = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
            checkpoint()
        finally:
            runtime_scope.close()
    print(f"Report: {output}")
    if metadata["status"] != "complete":
        return 2
    return 0 if all(r["passed_all_turns"] for r in reports) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as exc:
        print(f"[HARNESS ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
