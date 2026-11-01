"""Summarize correctness and cost by separate authored complexity dimensions."""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.runtime_support import write_json_atomic


def metrics_total(reports):
    values = [r.get("efficiency", {}) for r in reports]
    costs = [v.get("estimated_cost_usd") for v in values]
    return {
        "api_requests": sum(v.get("api_requests", 0) for v in values),
        "executed_tool_calls": sum(v.get("executed_tool_calls", 0) for v in values),
        "repeated_identical_calls": sum(v.get("repeated_identical_calls", 0) for v in values),
        "elapsed_turn_seconds": sum(v.get("elapsed_turn_seconds", 0) for v in values),
        "input_tokens_reported": sum(
            v.get("reported_tokens", {}).get("input_tokens", 0) for v in values
        ),
        "output_tokens_reported": sum(
            v.get("reported_tokens", {}).get("output_tokens", 0) for v in values
        ),
        "usage_complete": all(v.get("usage_complete", False) for v in values),
        "estimated_cost_usd": sum(costs) if all(c is not None for c in costs) else None,
    }


def summarize(report):
    rows, pairs, all_phases = [], [], []
    grouped = defaultdict(list)
    axes = (
        "turns",
        "input_files",
        "services",
        "records",
        "dependency_depth",
        "fault_types",
        "fault_count",
        "interaction",
    )
    for case in report["cases"]:
        shared = [case[k] for k in ("shared_prefix", "diagnostic_prefix") if case.get(k)]
        all_phases.extend(shared)
        all_phases.extend(case.get("diagnostic_continuations", []))
        for condition, phase in case["branches"].items():
            all_phases.append(phase)
            complexity = phase.get("scenario_contract", {}).get("complexity", case["complexity"])
            row = {
                "id": case["id"],
                "repetition": case.get("repetition"),
                "condition": condition,
                "branch_origin": case.get("branch_origin"),
                "status": phase["status"],
                "passed": phase["status"] == "passed",
                "complexity": complexity,
                "incremental_efficiency": phase.get("efficiency", {}),
            }
            rows.append(row)
            for axis in axes:
                value = json.dumps(complexity.get(axis), sort_keys=True)
                grouped[
                    (
                        case["complexity"]["family"],
                        condition,
                        case.get("branch_origin"),
                        axis,
                        value,
                    )
                ].append(row)
        if "baseline" in case["branches"] and "treatment" in case["branches"]:
            pairs.append(
                {
                    "id": case["id"],
                    "repetition": case.get("repetition"),
                    "origin": case.get("branch_origin"),
                    "baseline_passed": case["branches"]["baseline"]["status"] == "passed",
                    "treatment_passed": case["branches"]["treatment"]["status"] == "passed",
                    "shared_prefix_cost": metrics_total(shared),
                    "baseline_incremental_cost": metrics_total([case["branches"]["baseline"]]),
                    "treatment_incremental_cost": metrics_total([case["branches"]["treatment"]]),
                }
            )
    groups = []
    for (family, condition, origin, axis, value), members in grouped.items():
        groups.append(
            {
                "family": family,
                "condition": condition,
                "origin": origin,
                "axis": axis,
                "value": json.loads(value),
                "attempts": len(members),
                "passed": sum(r["passed"] for r in members),
                "pass_rate": sum(r["passed"] for r in members) / len(members),
                "mean_tool_calls": sum(
                    r["incremental_efficiency"].get("executed_tool_calls", 0) for r in members
                )
                / len(members),
            }
        )
    return {
        "rows": rows,
        "controlled_pairs": pairs,
        "by_complexity_dimension": groups,
        "total_actual_execution_cost": metrics_total(all_phases),
        "prefix_failures": sum(
            c.get("shared_prefix", {}).get("status") == "failed"
            for c in report["cases"]
            if c.get("shared_prefix")
        ),
        "interpretation": "Groups retain family, condition and prefix origin. Repetitions are descriptive. Axes can covary. Shared-prefix execution is charged once; each branch still pays for sending its full history. Repeated tool calls can be necessary polling or verification.",
    }


def analyze(path):
    path = Path(path)
    report = json.loads(path.read_text(encoding="utf-8"))
    result = summarize(report)
    write_json_atomic(path.parent / "metrics.json", result)
    lines = [
        "# V2 execution analysis\n",
        result["interpretation"] + "\n",
        "| Task | Condition | Prefix origin | Outcome | All executed tool calls | Repeated identical calls | API requests | Estimated USD |",
        "|---|---|---|---|---:|---:|---:|---:|",
    ]
    for row in result["rows"]:
        m = row["incremental_efficiency"]
        cost = m.get("estimated_cost_usd")
        lines.append(
            f"| {row['id']} | {row['condition']} | {row['branch_origin']} | {row['status']} | {m.get('executed_tool_calls', 0)} | {m.get('repeated_identical_calls', 0)} | {m.get('api_requests', 0)} | {cost if cost is not None else 'unpriced / incomplete usage'} |"
        )
    lines += [
        "\nDimension-level tables and paired outcomes are in metrics.json. Shared-prefix and diagnostic costs are included once in total_actual_execution_cost.\n"
    ]
    (path.parent / "analysis.md").write_text("\n".join(lines), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    analyze(parser.parse_args().report)
