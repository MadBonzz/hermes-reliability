"""Run v2 tasks and paired conditions with one sampled, reusable service prefix."""

import copy
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.benchmark_v2 import condition_spec, validate_prefix, validate_v2
from src.eval_runner import parse_args
from src.evaluation_metrics import load_prices
from src.runtime_support import sha256_file, workspace_manifest, write_json_atomic
from src.scenario_validation import load_scenarios, suite_sha256

ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def phase_workspace():
    """Allow short-lived Windows handle locks to clear after worker exit."""
    workspace = tempfile.TemporaryDirectory(prefix="")
    try:
        yield workspace.name
    finally:
        for attempt in range(5):
            try:
                workspace.cleanup()
                break
            except PermissionError as exc:
                if getattr(exc, "winerror", None) not in (32, 33) or attempt == 4:
                    raise
                time.sleep(attempt + 1)


def run_phase(config, key):
    out = Path(config["output"])
    out.mkdir(parents=True, exist_ok=False)
    path = out / "input.json"
    with (
        phase_workspace() as runtime_root,
        (out / "worker.log").open("w", encoding="utf-8") as log,
    ):
        write_json_atomic(path, {**config, "runtime_root": runtime_root})
        process = subprocess.run(
            [
                sys.executable,
                config.get("worker_path", str(ROOT / "src/eval_v2_worker.py")),
                str(path),
            ],
            env={**os.environ, "TASK_API_KEY": key},
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        write_json_atomic(
            out / "final_workspace.json", workspace_manifest(Path(runtime_root) / "workspace_1")
        )
    result_path = out / "report.json"
    if not result_path.exists():
        result = {
            "status": "error",
            "turns": [],
            "harness_errors": ["Worker produced no report"],
            "efficiency": {"usage_complete": False, "estimated_cost_usd": None},
        }
        write_json_atomic(result_path, result)
        return result
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if process.returncode:
        result["status"] = "error"
        result.setdefault("harness_errors", []).append(f"Worker exited {process.returncode}")
        write_json_atomic(result_path, result)
    return result


def run_case(spec, config, key, *, launch=run_phase, order_seed=0):
    """Prefix requests execute once. Every branch starts from the same deep copy."""
    experiment = spec.get("experiment")
    base = Path(config["output"])
    entry = {
        "id": spec["id"],
        "complexity": spec["complexity"],
        "branches": {},
        "shared_prefix": None,
    }
    branch_turn = experiment["branch_turn"] if experiment else 1
    continuation = None
    if branch_turn > 1:
        prefix_config = {
            **config,
            "scenario": spec,
            "output": str(base / "prefix"),
            "stop_before_turn": branch_turn,
        }
        prefix = launch(prefix_config, key)
        entry["shared_prefix"] = prefix
        if prefix["status"] == "error":
            entry["status"] = "error"
            return entry
        if prefix["status"] != "prefix_ready":
            entry["status"] = "prefix_failed"
            # Retain the sampled failure, then validate an authored prefix separately.
            if all("reference" in t for t in spec["turns"][: branch_turn - 1]):
                prefix = launch(
                    {
                        **prefix_config,
                        "output": str(base / "reference_prefix"),
                        "reference_until": branch_turn - 1,
                    },
                    key,
                )
                entry["diagnostic_prefix"] = prefix
                entry["branch_origin"] = "authored_reference"
                if prefix["status"] == "error":
                    entry["status"] = "error"
                    return entry
            else:
                return entry
        else:
            entry["branch_origin"] = "sampled_prefix"
        try:
            validate_prefix(prefix, spec)
        except ValueError as exc:
            entry["status"] = "unbranchable_prefix"
            entry["branch_error"] = str(exc)
            return entry
        continuation = prefix["continuation"]
    else:
        entry["branch_origin"] = "empty_prefix"
    conditions = ["baseline", "treatment"] if experiment else ["ordinary"]
    random.Random(order_seed).shuffle(conditions)
    entry["condition_order"] = conditions
    for condition in conditions:
        selected = condition_spec(spec, condition)
        selected["turns"] = [t for t in selected["turns"] if t["turn_number"] >= branch_turn]
        print(f"{spec['id']} / {condition}", flush=True)
        phase = {
            **config,
            "scenario": selected,
            "output": str(base / condition),
            "continuation": copy.deepcopy(continuation),
        }
        entry["branches"][condition] = launch(phase, key)
        report = entry["branches"][condition]
        if report["status"] == "error":
            entry["status"] = "error"
            return entry
        failed = report.get("failed_at_turn")
        if not experiment:
            diagnostics = []
            while failed and failed < len(spec["turns"]):
                if not all("reference" in t for t in spec["turns"][:failed]):
                    break
                diagnostic = launch(
                    {
                        **phase,
                        "scenario": copy.deepcopy(spec),
                        "output": str(base / f"reference_after_{failed}"),
                        "reference_until": failed,
                    },
                    key,
                )
                diagnostics.append(diagnostic)
                if diagnostic["status"] == "error":
                    entry["diagnostic_continuations"] = diagnostics
                    entry["status"] = "error"
                    return entry
                failed = diagnostic.get("failed_at_turn")
            entry["diagnostic_continuations"] = diagnostics
    entry["status"] = "complete"
    return entry


def main(argv=None):
    parser, args = parse_args(argv, v2=True)
    prices = load_prices(args.prices)
    suite, sources = load_scenarios(
        args.scenarios, ROOT / "scenario_v2.schema.json", args.suite_name
    )
    validate_v2(suite, ROOT / "scenario_v2.schema.json")
    scenarios = suite["scenarios"]
    if args.scenario_id:
        missing = set(args.scenario_id) - {s["id"] for s in scenarios}
        if missing:
            parser.error(f"Unknown scenario IDs: {sorted(missing)}")
        scenarios = [s for s in scenarios if s["id"] in args.scenario_id]
    if args.validate_only:
        print(
            f"Validated {len(scenarios)} v2 scenarios, {sum(bool(s.get('experiment')) for s in scenarios)} controlled pairs"
        )
        return 0
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    out = Path(args.output).resolve() if args.output else ROOT / "eval_harness/v2_runs" / run_id
    out.mkdir(parents=True, exist_ok=False)
    source = out / "source" / "src"
    source.mkdir(parents=True)
    for path in (ROOT / "src").glob("*.py"):
        shutil.copy2(path, source / path.name)
    shutil.copy2(ROOT / "scenario_v2.schema.json", out / "source" / "scenario_v2.schema.json")
    summary = {
        "benchmark_version": 2,
        "suite_name": suite["suite_name"],
        "run_id": run_id,
        "model": args.model,
        "suite_sha256": suite_sha256(suite),
        "scenario_sources": sources,
        "implementation_sha256": {p.name: sha256_file(p) for p in source.glob("*.py")},
        "schema_sha256": sha256_file(ROOT / "scenario_v2.schema.json"),
        "prices": prices,
        "order_seed": args.order_seed,
        "status": "running",
        "cases": [],
    }
    write_json_atomic(out / "report.json", summary)
    try:
        for repetition in range(args.repetitions):
            for spec in scenarios:
                config = {
                    "base_url": args.base_url,
                    "model": args.model,
                    "hermes_root": args.hermes_root,
                    "max_iterations": args.max_iterations,
                    "request_interval": args.request_interval,
                    "run_id": run_id,
                    "prices": prices,
                    "output": str(out / f"repeat_{repetition + 1}" / spec["id"]),
                    "worker_path": str(source / "eval_v2_worker.py"),
                }
                entry = run_case(
                    spec, config, args.api_key, order_seed=args.order_seed + repetition
                )
                entry["repetition"] = repetition + 1
                summary["cases"].append(entry)
                write_json_atomic(out / "report.json", summary)
                if entry["status"] == "error":
                    raise RuntimeError(f"Invalid execution in {spec['id']}. See phase reports.")
        summary["status"] = "complete"
    except KeyboardInterrupt:
        summary["status"] = "interrupted"
    except Exception as exc:
        summary.update(status="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        write_json_atomic(out / "report.json", summary)
    from src.analyze_v2 import analyze

    analyze(out / "report.json")
    print(f"Report: {out / 'report.json'}")
    return 0 if summary["status"] == "complete" else 2


if __name__ == "__main__":
    sys.exit(main())
