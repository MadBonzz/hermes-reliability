"""Index of benchmark results by agent source revision.

Each entry points at a saved report.json and records which framework revision
produced it, so a later campaign can find the results for its base version.
Orchestrator lanes register themselves; ordinary benchmark runs are added with
`scan`, which reads the revision recorded in each report's `hermes` field.

    uv run --locked python -m src.orchestrator.regression_index scan eval_harness/runs
    uv run --locked python -m src.orchestrator.regression_index list <revision>

The index stores paths and compact summaries only; reports stay where they are.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from src.evaluation.differential import report_summary
from src.evaluation.runtime_support import sha256_file, write_json_atomic

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX = ROOT / "eval_harness/regression_index.json"


def load(index_path):
    path = Path(index_path)
    if not path.exists():
        return {"version": 1, "entries": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("entries"), list):
        raise ValueError(f"{path}: entries must be a list")
    return data


def record(index_path, revision, report_path, **details):
    """Add (or refresh) the entry for one report and revision."""
    report_path = Path(report_path).resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    entry = {
        "revision": revision,
        "report": str(report_path),
        "report_sha256": sha256_file(report_path),
        "suite_sha256": report.get("suite_sha256"),
        "model": report.get("model"),
        "hermes_approval": report.get("hermes_approval"),
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "summary": report_summary(report),
        **details,
    }
    data = load(index_path)
    data["entries"] = [e for e in data["entries"] if e["report"] != entry["report"]] + [entry]
    write_json_atomic(Path(index_path), data)
    return entry


def lookup(index_path, revision):
    """Entries for a revision; a short prefix matches as with git."""
    if not revision:
        return []
    return [
        e
        for e in load(index_path)["entries"]
        if e["revision"] and (e["revision"] == revision or e["revision"].startswith(revision))
    ]


def scan(index_path, runs_dir):
    """Register every benchmark report under runs_dir that records its Hermes commit."""
    added = []
    for report_path in sorted(Path(runs_dir).rglob("report.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        commit = (report.get("hermes") or {}).get("commit")
        if not commit or "cases" not in report:
            continue
        added.append(
            record(
                index_path,
                commit,
                report_path,
                kind="benchmark_run",
                dirty=(report.get("hermes") or {}).get("dirty"),
            )
        )
    return added


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", default=str(DEFAULT_INDEX))
    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan", help="Register benchmark run reports")
    scan_parser.add_argument("runs_dir")
    list_parser = sub.add_parser("list", help="Show entries for a revision")
    list_parser.add_argument("revision")
    args = parser.parse_args(argv)
    if args.command == "scan":
        added = scan(args.index, args.runs_dir)
        print(f"Registered {len(added)} reports in {args.index}")
    else:
        for entry in lookup(args.index, args.revision):
            print(json.dumps({k: v for k, v in entry.items() if k != "summary"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
