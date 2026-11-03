"""Run benchmark tasks and paired conditions with one sampled, reusable service prefix."""

import argparse
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
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.evaluation.benchmark import condition_spec, validate_benchmark, validate_prefix
from src.evaluation.evaluation_metrics import load_prices
from src.evaluation.runtime_support import sha256_file, workspace_manifest, write_json_atomic
from src.evaluation.scenario_validation import DEFAULT_SUITE_NAME, load_scenarios, suite_sha256
from src.evaluation.utils import load_eval_config

ROOT = Path(__file__).resolve().parents[2]


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
                config.get("worker_path", str(ROOT / "src/evaluation/eval_worker.py")),
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
            validate_prefix(prefix, spec, runtime_kind=config.get("agent_kind", "hermes"))
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
        diagnostics = entry.setdefault("diagnostic_continuations", [])
        full_condition = condition_spec(spec, condition)
        while failed and failed < len(spec["turns"]):
            if not all("reference" in t for t in full_condition["turns"][:failed]):
                break
            label = (
                f"{condition}_reference_after_{failed}"
                if experiment
                else f"reference_after_{failed}"
            )
            diagnostic = launch(
                {
                    **phase,
                    "scenario": copy.deepcopy(full_condition),
                    "output": str(base / label),
                    "continuation": None,
                    "reference_until": failed,
                },
                key,
            )
            diagnostic["condition"] = condition
            diagnostics.append(diagnostic)
            if diagnostic["status"] == "error":
                entry["status"] = "error"
                return entry
            next_failed = diagnostic.get("failed_at_turn")
            if next_failed and next_failed <= failed:
                entry["status"] = "error"
                entry["branch_error"] = (
                    "Reference continuation failed to advance beyond its validated prefix"
                )
                return entry
            failed = next_failed
    entry["status"] = "complete"
    return entry


def parse_args(argv=None):
    config = load_eval_config(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description="Run property-oriented agent evaluations and controlled conditions."
    )
    parser.add_argument(
        "--scenarios",
        default=str(ROOT / "scenarios"),
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
        "--agent-kind", choices=["hermes", "openhands", "deerflow"], default="hermes"
    )
    parser.add_argument("--runtime-image", help="Pinned native Docker image for OpenHands/DeerFlow")
    parser.add_argument(
        "--memory-flush-timeout",
        type=int,
        default=30,
        help="DeerFlow memory drain deadline in seconds at checkpoint/restore/shutdown",
    )
    parser.add_argument(
        "--recursion-limit",
        type=int,
        default=100,
        help="DeerFlow native graph superstep limit, distinct from LLM iterations",
    )
    parser.add_argument(
        "--output", help="New run directory; defaults to eval_harness/runs/<run-id>"
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--prices")
    parser.add_argument("--order-seed", type=int, default=0)
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
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    if args.agent_kind != "hermes" and not args.runtime_image and not args.validate_only:
        parser.error("--runtime-image is required for a native container agent")
    if args.recursion_limit < 1:
        parser.error("--recursion-limit must be positive")
    if args.memory_flush_timeout < 1:
        parser.error("--memory-flush-timeout must be positive")
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
    prices = load_prices(args.prices)
    try:
        suite, sources = load_scenarios(
            args.scenarios,
            ROOT / "scenario.schema.json",
            args.suite_name,
            runtime_kind=args.agent_kind,
        )
        validate_benchmark(suite, ROOT / "scenario.schema.json", runtime_kind=args.agent_kind)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    scenarios = suite["scenarios"]
    if args.scenario_id:
        missing = set(args.scenario_id) - {s["id"] for s in scenarios}
        if missing:
            parser.error(f"Unknown scenario IDs: {sorted(missing)}")
        scenarios = [s for s in scenarios if s["id"] in args.scenario_id]
    if args.validate_only:
        print(
            f"Validated {len(scenarios)} benchmark scenarios, {sum(bool(s.get('experiment')) for s in scenarios)} controlled pairs"
        )
        return 0
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    out = Path(args.output).resolve() if args.output else ROOT / "eval_harness/runs" / run_id
    out.mkdir(parents=True, exist_ok=False)
    source = out / "source" / "src"
    source.mkdir(parents=True)
    shutil.copy2(ROOT / "src/__init__.py", source / "__init__.py")
    shutil.copytree(
        ROOT / "src/evaluation",
        source / "evaluation",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(ROOT / "scenario.schema.json", out / "source" / "scenario.schema.json")
    summary = {
        "benchmark_version": 2,
        "suite_name": suite["suite_name"],
        "run_id": run_id,
        "model": args.model,
        "agent_kind": args.agent_kind,
        "runtime_image": args.runtime_image,
        "memory_flush_timeout": args.memory_flush_timeout
        if args.agent_kind == "deerflow"
        else None,
        "suite_sha256": suite_sha256(suite),
        "scenario_sources": sources,
        "implementation_sha256": {
            p.relative_to(source).as_posix(): sha256_file(p) for p in source.rglob("*.py")
        },
        "schema_sha256": sha256_file(ROOT / "scenario.schema.json"),
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
                    "agent_kind": args.agent_kind,
                    "runtime_image": args.runtime_image,
                    "recursion_limit": args.recursion_limit,
                    "memory_flush_timeout": args.memory_flush_timeout,
                    "hermes_root": args.hermes_root,
                    "max_iterations": args.max_iterations,
                    "request_interval": args.request_interval,
                    "run_id": run_id,
                    "prices": prices,
                    "output": str(out / f"repeat_{repetition + 1}" / spec["id"]),
                    "worker_path": str(
                        source
                        / "evaluation"
                        / (
                            "eval_worker.py"
                            if args.agent_kind == "hermes"
                            else "eval_container_worker.py"
                        )
                    ),
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
    from src.evaluation.analyze import analyze

    analyze(out / "report.json")
    print(f"Report: {out / 'report.json'}")
    return 0 if summary["status"] == "complete" else 2


if __name__ == "__main__":
    sys.exit(main())
