"""One fresh Hermes runtime for a benchmark execution phase, with neutral visible paths."""

import json
import logging
import os
import sys
from contextlib import chdir
from importlib.metadata import distributions
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.evaluation import utils
from src.evaluation.api_trace import RequestPacer, SecretRedactor
from src.evaluation.benchmark import visible_input_validator
from src.evaluation.evaluation_metrics import efficiency, injection_evidence
from src.evaluation.execution_isolation import isolated_python
from src.evaluation.hermes_runner import run_single_scenario
from src.evaluation.runtime_support import workspace_manifest, write_json_atomic


def execute(config):
    spec = config["scenario"]
    out = Path(config["output"])
    key = os.environ.pop("TASK_API_KEY", "unused")
    redactor = SecretRedactor([key])
    continuation = config.get("continuation") or {}
    # The temp root, cwd, task identity and profile contain no scenario/run labels.
    # Explicit source inspection by arbitrary code remains outside the file path policy.
    for name in list(os.environ):
        if name.startswith("EVAL_") or name.startswith("EVALV2_"):
            os.environ.pop(name)
    root = Path(config["runtime_root"])
    with (
        chdir(root),
        isolated_python(root) as python_environment,
        utils.hermes_run_environment(root / "profile"),
    ):
        utils.load_hermes(config.get("hermes_root"))
        os.environ.pop("HERMES_AGENT_ROOT", None)
        report = run_single_scenario(
            spec,
            config["base_url"],
            config["model"],
            key,
            root,
            max_iterations=config["max_iterations"],
            run_id=config["run_id"],
            trace_root=out / "traces",
            redactor=redactor,
            checkpoint=lambda value: write_json_atomic(out / "report.json", redactor(value)),
            guard_file_paths=True,
            observe_native_controls=True,
            request_pacer=RequestPacer(config["request_interval"]),
            workspace_name="workspace_1",
            initial_history=continuation.get("history"),
            initial_services=continuation.get("services"),
            initial_fault_flags=continuation.get("fault_flags"),
            stop_before_turn=config.get("stop_before_turn"),
            reference_until=config.get("reference_until", 0),
            input_validator=visible_input_validator(spec, utils.PROJECT_ROOT),
        )
        report["final_workspace"] = workspace_manifest(root / "workspace_1")
        report["python_environment"] = python_environment
        library = (
            root / "python_env/Lib/site-packages"
            if os.name == "nt"
            else root
            / f"python_env/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        )
        report["python_environment"]["installed_packages"] = sorted(
            [
                {"name": d.metadata["Name"], "version": d.version}
                for d in distributions(path=[str(library)])
            ],
            key=lambda package: package["name"],
        )
        report["efficiency"] = efficiency(report, config.get("prices"))
        report["injection_evidence"] = injection_evidence(report, spec.get("observation"))
        report["branch_runtime"] = (
            "fresh_runtime_service_checkpoint" if continuation else "fresh_runtime"
        )
        write_json_atomic(out / "report.json", redactor(report))
        logging.shutdown()
    return report


if __name__ == "__main__":
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    result = execute(config)
    sys.exit(2 if result["status"] == "error" else 0)
