"""Validate generated benchmark suites through installed Hermes tools without task inference."""

import argparse
import sys
from pathlib import Path

if "--validate-revision" in sys.argv:
    sys.path.insert(0, sys.argv[sys.argv.index("--validate-revision") + 1])

from src.evaluation import hermes_runner


def validate_revision():
    import copy
    import logging
    import subprocess
    import tempfile
    from types import SimpleNamespace
    from unittest.mock import patch

    from src.evaluation import utils
    from src.evaluation.benchmark import condition_spec, validate_benchmark
    from src.evaluation.runtime_support import sha256_file, write_json_atomic
    from src.evaluation.scenario_validation import load_scenarios

    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-revision", type=Path, required=True)
    parser.add_argument("--hermes-root", required=True)
    parser.add_argument("--validation-output", type=Path, required=True)
    args = parser.parse_args()
    result = {"passed": False, "references": [], "validator_sha256": sha256_file(Path(__file__))}
    try:
        suite, _ = load_scenarios(
            args.validate_revision / "scenarios", args.validate_revision / "scenario.schema.json"
        )
        validate_benchmark(suite, args.validate_revision / "scenario.schema.json")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with utils.hermes_run_environment(root / "profile"):
                utils.load_hermes(args.hermes_root)

                def agent(*args, **kwargs):
                    tools = utils.model_tools.get_tool_definitions(
                        enabled_toolsets=["terminal", "file"], quiet_mode=True
                    )
                    return SimpleNamespace(
                        tools=tools, valid_tool_names={t["function"]["name"] for t in tools}
                    )

                with patch.object(hermes_runner, "instantiate_hermes_agent", agent):
                    for original in suite["scenarios"]:
                        for condition in (
                            ["baseline", "treatment"]
                            if original.get("experiment")
                            else ["ordinary"]
                        ):
                            spec = condition_spec(copy.deepcopy(original), condition)
                            total_turns = len(spec["turns"])
                            validation_turns = (
                                total_turns if "reference" in spec["turns"][-1] else total_turns - 1
                            )
                            if validation_turns == 0:
                                continue
                            spec["turns"] = spec["turns"][:validation_turns]
                            if not all("reference" in turn for turn in spec["turns"]):
                                raise ValueError("Every validation turn needs a runnable reference")
                            report = hermes_runner.run_single_scenario(
                                spec,
                                "http://127.0.0.1:9/v1",
                                "reference-validation",
                                "unused",
                                root / condition / original["id"],
                                trace_root=args.validation_output.parent / "traces" / condition,
                                guard_file_paths=True,
                                observe_native_controls=True,
                                workspace_name="workspace_1",
                                reference_until=len(spec["turns"]),
                            )
                            result["references"].append(
                                {
                                    "id": original["id"],
                                    "condition": condition,
                                    "validated_turns": validation_turns,
                                    "total_turns": total_turns,
                                    "full_task_example": validation_turns == total_turns,
                                    "status": report["status"],
                                    "harness_errors": report.get("harness_errors", []),
                                }
                            )
                            write_json_atomic(
                                args.validation_output.parent
                                / f"{original['id']}_{condition}.json",
                                report,
                            )
                logging.shutdown()
        tests = list((args.validate_revision / "tests").glob("test*.py"))
        result["test_files"] = [p.name for p in tests]
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
        write_json_atomic(args.validation_output, result)
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(validate_revision())
