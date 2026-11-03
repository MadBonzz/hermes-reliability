"""Validate generated benchmark suites with native tools in isolated containers."""

import argparse
import copy
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if "--validate-revision" in sys.argv:
    sys.path.insert(0, sys.argv[sys.argv.index("--validate-revision") + 1])

from src.evaluation.benchmark import condition_spec, validate_benchmark
from src.evaluation.eval_container_worker import execute
from src.evaluation.runtime_support import read_jsonl, sha256_file, write_json_atomic
from src.evaluation.scenario_validation import load_scenarios


def reference_diagnostics(report):
    """Expose failed authored actions as evidence, without guessing their cause."""
    failures = []
    tool_failures = []
    for turn in report.get("reference_prefix", []):
        for event in read_jsonl(turn["trace_path"]) if turn.get("trace_path") else []:
            if event.get("event") != "tool_complete":
                continue
            native = event.get("native_result")
            if not isinstance(native, dict):
                native = {}
            observation = native.get("observation", native)
            if not isinstance(observation, dict):
                observation = {}
            status = {
                k: observation[k]
                for k in ("is_error", "status", "exit_code", "timeout")
                if k in observation
            }
            if (
                event.get("error")
                or status.get("is_error") is True
                or status.get("status") == "error"
                or status.get("timeout") is True
                or (type(status.get("exit_code")) is int and status["exit_code"] != 0)
            ):
                tool_failures.append(
                    {
                        "turn_number": turn["turn_number"],
                        "gate_passed": turn.get("gate_passed"),
                        "id": event.get("id"),
                        "name": event.get("name"),
                        "result": event.get("result"),
                        "error": event.get("error"),
                        "native_status": status,
                        "trace_path": turn["trace_path"],
                    }
                )
        if turn.get("gate_passed"):
            continue
        failures.append(
            {
                "turn_number": turn["turn_number"],
                "checks": {
                    group: turn.get(group + "_check") for group in ("state", "process", "output")
                },
                "calls": [
                    {
                        key: call.get(key)
                        for key in (
                            "id",
                            "name",
                            "arguments",
                            "result",
                            "error",
                            "completed",
                            "native_observation_status",
                        )
                    }
                    for call in turn.get("execution_events", [])
                ],
                "harness_errors": turn.get("harness_errors", []),
                "runtime_errors": turn.get("runtime_errors", []),
                "artifact_checks": turn.get("artifact_checks", []),
                "trace_path": turn.get("trace_path"),
            }
        )
    return {
        "workspace": report.get("workspace"),
        "native_runtime": report.get("native_runtime"),
        "failed_reference_turns": failures,
        "reported_tool_failures": tool_failures,
        "interpretation": "Tool errors and nonzero command exits remain visible even when the reference gate passed. They may be intentional or may expose a weak oracle; inspect the contract before attributing them. Native flags are read from captured callbacks, including for older evaluator copies that omitted them from summaries.",
        "runtime_errors": report.get("runtime_errors", []),
        "status_before_cleanup": report.get("status_before_cleanup"),
        "cleanup_error": report.get("cleanup_error"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-revision", type=Path, required=True)
    parser.add_argument("--validation-output", type=Path, required=True)
    parser.add_argument("--agent-kind", choices=["openhands", "deerflow"], required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--request-interval", type=float, default=0)
    parser.add_argument("--memory-flush-timeout", type=int, default=30)
    args = parser.parse_args(argv)
    if args.memory_flush_timeout < 1:
        parser.error("--memory-flush-timeout must be positive")
    result = {
        "passed": False,
        "references": [],
        "validator_sha256": sha256_file(Path(__file__)),
        "reference_origin": "authored task actions; real native auxiliary model calls if any",
    }
    key = os.environ.pop("TASK_API_KEY", "unused")
    try:
        result["reference_tool_profile"] = args.agent_kind
        suite, _ = load_scenarios(
            args.validate_revision / "scenarios",
            args.validate_revision / "scenario.schema.json",
            runtime_kind=args.agent_kind,
        )
        validate_benchmark(
            suite, args.validate_revision / "scenario.schema.json", runtime_kind=args.agent_kind
        )
        for original in suite["scenarios"]:
            for condition in (
                ["baseline", "treatment"] if original.get("experiment") else ["ordinary"]
            ):
                spec = condition_spec(copy.deepcopy(original), condition)
                for tool in spec.get("mock_tools", []):
                    if tool["behavior"]["type"] == "unverified_race_deduct":
                        raise ValueError(
                            "This native adapter does not implement cross-process file-race mocks"
                        )
                for turn in spec["turns"]:
                    for op in turn.get("pre_turn_perturbations", []) + turn.get(
                        "post_turn_cleanup", []
                    ):
                        if op["type"] in {"compress_context", "truncate_history"} and (
                            op.get("keep_last_n_messages", 0)
                            or op.get("use_native_compressor", False)
                        ):
                            raise ValueError(
                                "This native adapter supports only zero-history truncation; forced compression is unavailable"
                            )
                total_turns = len(spec["turns"])
                validation_turns = (
                    total_turns if "reference" in spec["turns"][-1] else total_turns - 1
                )
                if validation_turns == 0:
                    continue
                spec["turns"] = spec["turns"][:validation_turns]
                if not all("reference" in turn for turn in spec["turns"]):
                    raise ValueError("Every prerequisite turn needs a runnable reference")
                out = args.validation_output.parent / "references" / original["id"] / condition
                with tempfile.TemporaryDirectory() as temporary:
                    os.environ["TASK_API_KEY"] = key
                    report = execute(
                        {
                            "scenario": spec,
                            "runtime_root": temporary,
                            "output": str(out),
                            "agent_kind": args.agent_kind,
                            "runtime_image": args.runtime_image,
                            "base_url": args.base_url,
                            "model": args.model,
                            "run_id": "reference_validation",
                            "request_interval": args.request_interval,
                            "memory_flush_timeout": args.memory_flush_timeout,
                            "reference_until": len(spec["turns"]),
                        }
                    )
                result["references"].append(
                    {
                        "id": original["id"],
                        "condition": condition,
                        "validated_turns": validation_turns,
                        "total_turns": total_turns,
                        "full_task_example": validation_turns == total_turns,
                        "status": report["status"],
                        "harness_errors": report["harness_errors"],
                        "report": str(out / "report.json"),
                        "efficiency": report["efficiency"],
                        "diagnostics": reference_diagnostics(report),
                    }
                )
        tests = list((args.validate_revision / "tests").glob("test*.py"))
        result["test_files"] = [path.name for path in tests]
        result["tests_exit_code"] = 0
        if tests:
            process = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                cwd=args.validate_revision,
                check=False,
            )
            result["tests_exit_code"] = process.returncode
        result["passed"] = result["tests_exit_code"] == 0 and all(
            r["status"] == "passed" for r in result["references"]
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        os.environ.pop("TASK_API_KEY", None)
        write_json_atomic(args.validation_output, result)
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
