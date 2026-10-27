#!/usr/bin/env python3
"""
Declarative Multi-Turn Evaluation Runner for Hermes Agent.
Ingests a standardized JSON scenario suite, dynamically registers/cleans up mock tools,
applies turn-level perturbations (including native Hermes context compression),
and enforces 3-tier (State, Process, Output) gates after every turn.
"""

import argparse
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
from src.api_trace import APITrace, SecretRedactor
from src.checks import evaluate_output_checks, evaluate_process_checks, evaluate_state_checks
from src.mock_registry import DynamicMockToolManager
from src.perturbations import apply_perturbations, force_unlock_workspace
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
    hermes_workspace,
    instantiate_hermes_agent,
    load_eval_config,
    load_hermes,
    resolve_ws_path,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_turn(
    turn, *, agent, history, workspace, manager, report, trace_root, checkpoint, api_trace=None
):
    """Execute and checkpoint one turn, always applying its post-turn cleanup."""
    run_id, sid, task_id = report["run_id"], report["id"], report["task_id"]
    number = turn["turn_number"]
    prompt = turn["prompt"].replace("{WORKSPACE}", workspace.as_posix())
    marker = f"[EVAL run={run_id} scenario={sid} turn={number}]"
    sent_prompt = marker + "\n" + prompt
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
        "perturbations": [],
        "cleanup": [],
        "tool_calls": [],
        "final_answer": "",
    }
    report["turns"].append(record)
    agent.tool_start_callback = trace.tool_start
    agent.tool_complete_callback = trace.tool_complete
    trace.record(
        "turn_start",
        run_id=run_id,
        scenario_id=sid,
        turn_number=number,
        prompt=sent_prompt,
        input_history=history,
        tool_catalog=check_tool_catalog(agent, manager.registered_schemas),
    )
    start = time.monotonic()
    try:
        if api_trace:
            api_trace.set_context(number, "pre_turn_perturbations")
        apply_perturbations(
            turn.get("pre_turn_perturbations", []),
            workspace,
            manager.fault_flags,
            history,
            agent,
            record["perturbations"],
        )
        trace.record(
            "before_agent",
            history=history,
            perturbations=record["perturbations"],
            workspace=workspace_manifest(workspace),
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
            if result.get("error"):
                record["agent_error"] = str(result["error"])
        except Exception as exc:
            record["agent_error"] = f"{type(exc).__name__}: {exc}"
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
        for key, evaluator, value in (
            ("state", evaluate_state_checks, workspace),
            ("process", evaluate_process_checks, record["tool_calls"]),
            ("output", evaluate_output_checks, record["final_answer"]),
        ):
            ok, details = evaluator(turn.get(key + "_checks", []), value)
            record[key + "_check"] = {"passed": ok, "details": details}
        record["workspace_after_agent"] = workspace_manifest(workspace)
    except Exception as exc:
        record["harness_errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
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
):
    run_id = run_id or uuid.uuid4().hex
    sid = scenario["id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        raise ValueError(f"Unsafe scenario ID: {sid!r}")
    workspace = resolve_ws_path(workspace_root, sid)
    report = {
        "id": sid,
        "name": scenario["name"],
        "property_tested": scenario.get("property_tested", ""),
        "run_id": run_id,
        "workspace": str(workspace),
        "status": "running",
        "started_at_utc": utc_now(),
        "passed_all_turns": False,
        "failed_at_turn": None,
        "turns": [],
        "harness_errors": [],
    }
    manager = DynamicMockToolManager(
        workspace,
        {},
        registry=utils.registry,
        toolsets=utils.toolsets,
        model_tools=utils.model_tools,
    )
    workspace_scope = ExitStack()
    task_id = f"{run_id}_{sid}"
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
                )
            )
            report["api_trace_path"] = str(api_trace.path)
        manager.register_scenario_mocks(scenario.get("mock_tools", []))
        agent = instantiate_hermes_agent(base_url, model_name, api_key, max_iterations, workspace)
        manager.attach_to_agent_instance(agent)
        report["tool_catalog"] = check_tool_catalog(agent, manager.registered_schemas)
        history = []
        report["passed_all_turns"] = True
        for turn in scenario["turns"]:
            history, record = _run_turn(
                turn,
                agent=agent,
                history=history,
                workspace=workspace,
                manager=manager,
                report=report,
                trace_root=trace_root,
                checkpoint=checkpoint,
                api_trace=api_trace,
            )
            number = turn["turn_number"]
            print(
                f"  {sid} turn {number}: {'PASS' if record['gate_passed'] else 'FAIL'} ({len(record['tool_calls'])} calls)"
            )
            if not record["gate_passed"]:
                report["passed_all_turns"] = False
                report["failed_at_turn"] = number
                report["status"] = (
                    "error" if record["harness_errors"] or record["agent_error"] else "failed"
                )
                break
        else:
            report["status"] = "passed"
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
        tool_calls=sum(len(t["tool_calls"]) for t in turns),
        reports=reports,
        **metadata,
    )


def parse_args(argv=None):
    config = load_eval_config(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(
        description="Run declarative multi-turn evaluations on Hermes Agent."
    )
    parser.add_argument(
        "--scenarios",
        default=str(PROJECT_ROOT / "scenarios"),
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
        "--validate-only",
        action="store_true",
        help="Validate suite without importing Hermes or contacting a model server",
    )
    args = parser.parse_args(argv)
    if args.max_iterations <= 0:
        parser.error("--max-iterations must be positive")
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
        "max_iterations": args.max_iterations,
        "workspace_root": str(workspace_root),
        "trace_root": str(run_dir / "traces"),
        "keep_workspace": args.keep_workspace,
        "api_trace_format_version": 1,
    }
    redactor = SecretRedactor([args.api_key])

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
        root = load_hermes(args.hermes_root)
        metadata["hermes_root"] = str(root)
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
        )
        metadata["hermes_revision"] = revision.stdout.strip() if revision.returncode == 0 else None
        for scenario in scenarios:
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
            )
            if report["status"] == "error":
                # Infrastructure failure invalidates continued model comparison.
                metadata["status"] = "error"
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
                for report in reports:
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
