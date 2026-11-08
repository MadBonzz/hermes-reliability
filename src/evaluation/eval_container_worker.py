"""One benchmark phase using a real native agent in a disposable Docker container.

The provider key, scenario behavior and assertions remain on the host. Only
interfaces, task inputs and actual tool results are delivered to the runtime.
"""

import base64
import copy
import json
import os
import sys
import threading
import time
import traceback
from collections import Counter
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):
    __package__ = "src.evaluation"
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from .agent_container import AgentContainer, NativeExecutionError, inference_relay
from .api_trace import APITrace, RequestPacer, SecretRedactor
from .artifact_checks import evaluate_artifact_check
from .benchmark import visible_input_validator
from .checks import (
    evaluate_output_checks,
    evaluate_process_checks,
    evaluate_state_checks,
)
from .evaluation_metrics import efficiency, injection_evidence, native_tool_status
from .mock_registry import DynamicMockToolManager
from .runtime_support import TurnTrace, workspace_manifest, write_json_atomic


def materialize(root, snapshot):
    """Create an assertion-only mirror; never follow guest symlinks on the host."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    validated = {}
    for name, value in snapshot.items():
        if name == ".":
            continue
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
            raise ValueError(
                f"Guest artifact cannot be represented safely on the verifier host: {name!r}"
            )
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path == root:
            raise ValueError("Guest artifact escapes assertion mirror")
        if value["kind"] not in {"file", "directory"}:
            raise ValueError(f"Unscored guest artifact kind: {value['kind']} at {name}")
        validated[name] = (path, value)
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Assertion mirror contains an unexpected link")
        name = path.relative_to(root).as_posix()
        wanted = validated.get(name)
        if not wanted or (path.is_dir() != (wanted[1]["kind"] == "directory")):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
    for path, value in validated.values():
        if value["kind"] == "directory":
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = (
                value["text"].encode("utf-8")
                if "text" in value
                else base64.b64decode(value["data_base64"], validate=True)
            )
            path.write_bytes(raw)


def file_changes(before, root):
    after = workspace_manifest(root)
    changes = []
    for name, item in after.items():
        if name == "." or item == before.get(name):
            continue
        if item["kind"] == "directory":
            changes.append({"type": "mkdir", "path": name})
        else:
            changes.append(
                {
                    "type": "write_file",
                    "path": name,
                    "data_base64": base64.b64encode((Path(root) / name).read_bytes()).decode(
                        "ascii"
                    ),
                }
            )
    for name in before.keys() - after.keys():
        if before[name]["kind"] == "file":
            changes.append({"type": "delete_file", "path": name})
    return changes


def output_snapshot(auxiliary):
    """Select native outputs without aliasing similarly named workspace paths."""
    if "outputs" in auxiliary and auxiliary["outputs"].get("kind") != "directory":
        raise ValueError("Native outputs root must be a directory, not a link or file")
    return {
        name.removeprefix("outputs/"): value
        for name, value in auxiliary.items()
        if name.startswith("outputs/")
    }


def execute(config):
    spec = config["scenario"]
    out = Path(config["output"])
    key = os.environ.pop("TASK_API_KEY", "unused")
    redact = SecretRedactor([key])
    root = Path(config["runtime_root"]) / "workspace_1"
    root.mkdir(parents=True)
    flags = {}
    manager = DynamicMockToolManager(root, flags, registry=None, toolsets=None, model_tools=None)
    specs = spec.get("mock_tools", [])
    for tool in specs:
        behavior = tool["behavior"]
        if behavior["type"] == "unverified_race_deduct":
            raise ValueError(
                "Native container adapter does not implement cross-process file-race mocks"
            )
        if behavior["type"] == "stateful_service" and "initial_state" in behavior:
            manager.services.register(behavior)
    handlers = {tool["name"]: manager._build_handler(tool) for tool in specs}
    mock_lock = threading.RLock()
    trace = TurnTrace(out / "setup.jsonl", redact)
    active = {}
    executed = []
    calls = []
    attempted_counts, started_counts = Counter(), Counter()
    report = {
        "id": spec["id"],
        "name": spec["name"],
        "property_tested": spec["property_tested"],
        "scenario_contract": copy.deepcopy(spec),
        "run_id": config["run_id"],
        "status": "running",
        "turns": [],
        "reference_prefix": [],
        "harness_errors": [],
        "runtime_errors": [],
        "passed_all_turns": False,
        "failed_at_turn": None,
        "api_trace_path": str(out / "api.jsonl"),
        "workspace": "/workspace",
    }

    def save():
        write_json_atomic(out / "report.json", redact(report))

    def observe(event):
        value = dict(event)
        kind = value.pop("event")
        trace.record(kind, **value)
        if kind == "tool_attempt":
            calls.append({k: event[k] for k in ("id", "name", "arguments")})
            attempted_counts[event["id"]] += 1
        elif kind == "tool_start":
            started_counts[event["id"]] += 1
            # Older bridges and native action callbacks may have no separate
            # model boundary hook. Keep those calls without duplicating attempts.
            # Match by native ID because native middleware can rewrite arguments.
            if started_counts[event["id"]] > attempted_counts[event["id"]]:
                calls.append({k: event[k] for k in ("id", "name", "arguments")})
            active[event["id"]] = {
                "id": event["id"],
                "name": event["name"],
                "arguments": event["arguments"],
                "workspace_before": event.get("workspace"),
                "auxiliary_workspace_before": event.get("auxiliary_workspace", {}),
                "mock_before": manager.services.snapshot(),
                "completed": False,
            }
            executed.append(active[event["id"]])
            trace.record(
                "tool_state_before",
                id=event["id"],
                workspace=event.get("workspace", {}),
                mock_snapshot=active[event["id"]]["mock_before"],
                fault_flags=dict(flags),
                auxiliary_workspace=event.get("auxiliary_workspace", {}),
            )
        elif kind == "tool_complete":
            if event["id"] not in active:
                raise RuntimeError("Native tool result has no observed start")
            active[event["id"]].update(
                result=event.get("result"),
                completed=True,
                workspace_after=event.get("workspace"),
                auxiliary_workspace_after=event.get("auxiliary_workspace", {}),
                mock_after=manager.services.snapshot(),
                error=event.get("error"),
                native_observation_status=native_tool_status(event.get("native_result")),
            )
            trace.record(
                "tool_state_after",
                id=event["id"],
                workspace=event.get("workspace", {}),
                mock_snapshot=active[event["id"]]["mock_after"],
                fault_flags=dict(flags),
                auxiliary_workspace=event.get("auxiliary_workspace", {}),
            )

    def mock(payload):
        name = payload["name"]
        behavior = next(tool["behavior"] for tool in specs if tool["name"] == name)
        if behavior["type"] == "stateful_service":
            result = handlers[name](payload["arguments"])
            trace.record(
                "mock_dispatch",
                name=name,
                arguments=payload["arguments"],
                result=result,
                mock_snapshot=manager.services.snapshot(),
            )
            return {"result": result}
        # File-backed mocks are serialized remote service transactions. The
        # deliberately non-atomic race mock is rejected rather than made safe.
        with mock_lock:
            materialize(root, payload["workspace"])
            before = workspace_manifest(root)
            result = handlers[name](payload["arguments"])
            changes = file_changes(before, root)
            trace.record(
                "mock_dispatch",
                name=name,
                arguments=payload["arguments"],
                result=result,
                file_changes=changes,
            )
            return {"result": result, "file_changes": changes}

    target = {"model": config["model"], "base_url": config["base_url"]}
    relay_cancel = threading.Event()
    container = AgentContainer(
        config["runtime_image"],
        out,
        lambda payload: inference_relay(target, key, payload, cancel_event=relay_cancel),
        mock,
        observe,
    )
    container.cancel_inference = relay_cancel.set

    def perturb(operations):
        evidence = []
        for operation in operations:
            kind = operation["type"]
            event = copy.deepcopy(operation)
            if kind == "set_fault_flag":
                flags[operation["flag_name"]] = bool(operation.get("flag_value", True))
            elif kind == "set_mock_state":
                manager.services.patch(operation["service"], operation["values"])
            elif kind in {"truncate_history", "compress_context"}:
                if kind == "compress_context" and operation.get("use_native_compressor"):
                    raise ValueError("Forced native compression is not implemented by this adapter")
                event.update(
                    container.call(
                        "truncate_history",
                        keep_last_n_messages=operation.get("keep_last_n_messages", 0),
                    )
                )
            else:
                container.call("files", operations=[operation])
            event["applied"] = True
            evidence.append(event)
            trace.record("perturbation", **event)
        return evidence

    with APITrace(
        out / "api.jsonl",
        run_id=config["run_id"],
        scenario_id=spec["id"],
        redactor=redact,
        pacer=RequestPacer(config.get("request_interval", 0)),
        input_validator=visible_input_validator(spec, Path(__file__).resolve().parents[2]),
    ) as api:
        try:
            container.start()
            init = container.call(
                "init",
                config={
                    "agent_kind": config["agent_kind"],
                    "model": config["model"],
                    "max_iterations": config.get("max_iterations", 15),
                    "recursion_limit": config.get("recursion_limit", 100),
                    "memory_flush_timeout": config.get("memory_flush_timeout", 30),
                    "tools": [
                        {k: tool[k] for k in ("name", "description", "parameters")}
                        for tool in specs
                    ],
                },
            )
            report.update(
                workspace=init["workspace"],
                native_runtime=init["runtime_profile"],
                tool_catalog=init["tools"],
            )
            requested_flush = config.get("memory_flush_timeout", 30)
            if (
                config["agent_kind"] == "deerflow"
                and requested_flush != 30
                and init["runtime_profile"].get("memory_flush_timeout_seconds") != requested_flush
            ):
                raise ValueError("Native bridge did not apply the requested memory flush timeout")
            report["initial_workspace"] = container.call("snapshot")
            continuation = config.get("continuation")
            if continuation:
                manager.services.restore(continuation["services"])
                flags.update(continuation["fault_flags"])
                container.call("import", state=continuation["native_state"])
            reference_until = config.get("reference_until", 0)
            for turn in spec["turns"]:
                number = turn["turn_number"]
                if config.get("stop_before_turn") == number:
                    api.set_context(number - 1, "checkpoint")
                    state = container.call("export")
                    report.update(
                        status="prefix_ready",
                        continuation={
                            "history": state["history"],
                            "native_state": state,
                            "services": manager.services.checkpoint(),
                            "fault_flags": dict(flags),
                        },
                    )
                    break
                reference = number <= reference_until
                trace = TurnTrace(out / "traces" / f"turn_{number}.jsonl", redact)
                active, executed, calls = {}, [], []
                attempted_counts, started_counts = Counter(), Counter()
                api.set_context(number, "pre_turn_perturbations")
                record = {
                    "turn_number": number,
                    "prompt": turn["prompt"],
                    "sent_prompt": turn["prompt"].replace("{WORKSPACE}", init["workspace"]),
                    "origin": "authored_reference" if reference else "model",
                    "trace_path": str(trace.path),
                    "harness_errors": [],
                    "gate_passed": False,
                }
                record["prompt"] = record["sent_prompt"]
                records = report["reference_prefix"] if reference else report["turns"]
                records.append(record)
                started = time.monotonic()
                try:
                    trace.record(
                        "turn_start",
                        run_id=config["run_id"],
                        scenario_id=spec["id"],
                        turn_number=number,
                        prompt=record["sent_prompt"],
                        input_history=container.call("history"),
                        tool_catalog=init["tools"],
                        origin=record["origin"],
                        native_control_observation={
                            "mode": "not_instrumented",
                            "framework": config["agent_kind"],
                        },
                    )
                    record["perturbations"] = perturb(turn.get("pre_turn_perturbations", []))
                    before = container.call("snapshot")
                    record["workspace_before_agent"] = before
                    record["auxiliary_workspace_before_agent"] = container.call(
                        "auxiliary_snapshot"
                    )
                    trace.record(
                        "before_agent",
                        workspace=before,
                        mock_snapshot=manager.services.snapshot(),
                        sent_prompt=record["sent_prompt"],
                        auxiliary_workspace=record["auxiliary_workspace_before_agent"],
                    )
                    if reference:
                        api.set_context(number, "reference")
                        authored = json.loads(
                            json.dumps(turn["reference"]).replace("{WORKSPACE}", init["workspace"])
                        )
                        result = container.call(
                            "reference",
                            prompt=record["sent_prompt"],
                            actions=authored["actions"],
                            final_response=authored["final_response"],
                        )
                    else:
                        api.set_context(number, "agent")
                        result = container.call("run", prompt=record["sent_prompt"])
                    result.setdefault("messages", container.call("history"))
                    trace.record("agent_result", result=result)
                    api.raise_if_failed()
                    after = container.call("snapshot")
                    after_auxiliary = container.call("auxiliary_snapshot")
                    record["auxiliary_workspace_after_agent"] = after_auxiliary
                    services = manager.services.snapshot()
                    materialize(root, after)
                    artifact_roots, before_artifacts = {}, {}
                    if config["agent_kind"] == "deerflow" and any(
                        check.get("artifact_scope") == "outputs"
                        for check in turn.get("state_checks", [])
                    ):
                        outputs_root = root.parent / "outputs"
                        materialize(outputs_root, output_snapshot(after_auxiliary))
                        artifact_roots["outputs"] = outputs_root
                        before_artifacts["outputs"] = output_snapshot(
                            record["auxiliary_workspace_before_agent"]
                        )
                    state_ok, state_details = True, []
                    artifact_results = record["artifact_checks"] = []
                    for check_index, check in enumerate(turn.get("state_checks", [])):
                        if check["type"] == "artifact_python":
                            api.set_context(number, "artifact_verification")
                            checked = evaluate_artifact_check(
                                check,
                                after,
                                image=config["runtime_image"],
                                workspace=init["physical_workspace"],
                                visible_workspace=init["workspace"],
                                output=out / f"artifact_turn_{number}_check_{check_index}",
                            )
                            artifact_results.append(checked)
                            ok = checked["passed"]
                            details = [
                                f"[{'PASS' if ok else 'FAIL'}] artifact_python({check['name']}): exit={checked['exit_code']}, timed_out={checked['timed_out']}"
                            ]
                            trace.record("artifact_check", **checked)
                        else:
                            ok, details = evaluate_state_checks(
                                [check],
                                root,
                                mock_snapshot=services,
                                before_workspace=before,
                                artifact_roots=artifact_roots,
                                before_artifacts=before_artifacts,
                            )
                        state_ok = state_ok and ok
                        state_details.extend(details)
                    process_ok, process_details = evaluate_process_checks(
                        turn.get("process_checks", []), calls
                    )
                    output_ok, output_details = evaluate_output_checks(
                        turn.get("output_checks", []), result.get("final_response", "")
                    )
                    record.update(
                        workspace_after_agent=after,
                        mock_snapshot=services,
                        execution_events=copy.deepcopy(executed),
                        tool_calls=copy.deepcopy(calls),
                        final_answer=result.get("final_response", ""),
                        agent_termination={
                            "completed": result["completed"],
                            "native_status": result["native_status"],
                        },
                        state_check={"passed": state_ok, "details": state_details},
                        process_check={"passed": process_ok, "details": process_details},
                        output_check={"passed": output_ok, "details": output_details},
                        artifact_checks=artifact_results,
                        native_control_events=[],
                        native_control_coverage="not_instrumented",
                        gate_passed=bool(
                            result["completed"] and state_ok and process_ok and output_ok
                        ),
                    )
                    trace.record(
                        "checks",
                        **{k: record[k] for k in ("state_check", "process_check", "output_check")},
                    )
                    if reference and not record["gate_passed"]:
                        raise ValueError("Authored reference failed its required assertions")
                except NativeExecutionError as exc:
                    record["agent_error"] = {
                        "message": exc.native_message,
                        "operation": exc.operation,
                        "traceback": exc.native_traceback,
                    }
                    raise
                except Exception as exc:
                    record["harness_errors"].append(f"{type(exc).__name__}: {exc}")
                    raise
                finally:
                    record["elapsed_sec"] = time.monotonic() - started
                    record["execution_events"] = copy.deepcopy(executed)
                    record["tool_calls"] = copy.deepcopy(calls)
                    record["tool_call_coverage"] = {
                        "observed_tool_attempts": sum(attempted_counts.values()),
                        "execution_callbacks": len(executed),
                        "attempts_without_execution_callback": sum(
                            (attempted_counts - started_counts).values()
                        ),
                        "interpretation": "A model attempt can be rejected before the execution observer. Missing callbacks do not by themselves identify a refusal; inspect native tool responses. Callback-only tools remain included as a fallback. Internal native guards are not instrumented.",
                    }
                    record["mock_snapshot"] = manager.services.snapshot()
                    try:
                        if "workspace_after_agent" not in record:
                            try:
                                record["workspace_after_agent"] = container.call("snapshot")
                            except Exception as exc:
                                record["harness_errors"].append(f"Turn snapshot failed: {exc}")
                        try:
                            if "auxiliary_workspace_after_agent" not in record:
                                record["auxiliary_workspace_after_agent"] = container.call(
                                    "auxiliary_snapshot"
                                )
                        except Exception as exc:
                            record["harness_errors"].append(f"Auxiliary snapshot failed: {exc}")
                        api.set_context(number, "post_turn_cleanup")
                        record["cleanup"] = perturb(turn.get("post_turn_cleanup", []))
                    finally:
                        save()
                if record["harness_errors"]:
                    raise RuntimeError(
                        "Incomplete turn evidence: " + "; ".join(record["harness_errors"])
                    )
                if not record["gate_passed"]:
                    report.update(status="failed", failed_at_turn=number)
                    break
            else:
                report.update(status="passed", passed_all_turns=True)
        except NativeExecutionError as exc:
            report["status"] = "error"
            report["runtime_errors"].append(
                {
                    "message": exc.native_message,
                    "operation": exc.operation,
                    "traceback": exc.native_traceback,
                }
            )
        except Exception as exc:
            report["status"] = "error"
            report["harness_errors"].append(f"{type(exc).__name__}: {exc}")
        finally:
            report["status_before_cleanup"] = report["status"]
            try:
                report["final_workspace"] = container.call("snapshot")
                report["final_auxiliary_workspace"] = container.call("auxiliary_snapshot")
            except Exception as exc:
                report["harness_errors"].append(f"Final snapshot failed: {exc}")
            try:
                api.set_context(None, "shutdown")
                container.close()
            except Exception as exc:
                report["harness_errors"].append(f"Native cleanup failed: {exc}")
                report["cleanup_error"] = {
                    "type": type(exc).__name__,
                    "traceback": "".join(traceback.format_exception(exc)),
                }
            if report["harness_errors"]:
                report["status"] = "error"
            save()
    report["efficiency"] = efficiency(report, config.get("prices"))
    report["injection_evidence"] = injection_evidence(report, spec.get("observation"))
    report["branch_runtime"] = (
        "fresh_native_checkpoint" if config.get("continuation") else "fresh_native_runtime"
    )
    save()
    return report


if __name__ == "__main__":
    result = execute(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    sys.exit(2 if result["status"] == "error" else 0)
