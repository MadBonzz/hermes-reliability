"""Fresh-process bridge; does not modify the existing Hermes runner."""

import argparse
import json
from pathlib import Path

from src import eval_runner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orchestrator-request", type=Path, required=True)
    parser.add_argument("--orchestrator-result", type=Path, required=True)
    parser.add_argument("--hermes-root", required=True)
    parser.add_argument("--max-iterations", default="15")
    parser.add_argument("--request-interval", default="0")
    args = parser.parse_args()
    request = json.loads(args.orchestrator_request.read_text(encoding="utf-8"))
    output = Path(request["output_directory"])
    report_path = output / "eval_report.json"
    code = eval_runner.main(
        [
            "--scenarios",
            request["scenarios"],
            "--output",
            str(report_path),
            "--workspace-root",
            str(output / "workspaces"),
            "--base-url",
            request["target"]["base_url"],
            "--model",
            request["target"]["model"],
            "--hermes-root",
            args.hermes_root,
            "--max-iterations",
            args.max_iterations,
            "--request-interval",
            args.request_interval,
        ]
    )
    # Exit 1 means completed tests with failed gates, precisely the evidence the
    # orchestrator needs. Exit 2 denotes an execution/harness failure.
    if code not in (0, 1):
        return code
    report = json.loads(report_path.read_text(encoding="utf-8"))
    # Runner traces can be outside a custom report directory. Copy evidence into
    # this campaign, preserving the originals and recording their original paths.
    import shutil

    trace_root = Path(report["trace_root"])
    destination = output / "captured_traces"
    shutil.copytree(trace_root, destination)
    files = [report_path, *sorted(destination.rglob("*.jsonl"))]
    manifest = {
        "status": "complete",
        "original_trace_root": str(trace_root),
        "evidence_files": [str(p.relative_to(args.orchestrator_result.parent)) for p in files],
    }
    args.orchestrator_result.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
