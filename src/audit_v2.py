"""Independent checks of v2 wire evidence, branch inputs and cost aggregation."""

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analyze_v2 import metrics_total
from src.audit_api_evidence import audit
from src.evaluation_metrics import efficiency
from src.runtime_support import read_jsonl, write_json_atomic


def audit_v2(path):
    path = Path(path)
    summary = json.loads(path.read_text(encoding="utf-8"))
    errors, phases = [], []
    for case in summary["cases"]:
        prefixes = [case[k] for k in ("shared_prefix", "diagnostic_prefix") if case.get(k)]
        reports = (
            prefixes + list(case["branches"].values()) + case.get("diagnostic_continuations", [])
        )
        for report in reports:
            api_path = report.get("api_trace_path")
            if not api_path:
                errors.append(f"{case['id']}: missing API trace path")
                continue
            wire = audit(api_path)
            errors.extend(wire["errors"])
            recomputed = efficiency(report, summary.get("prices"))
            saved = report.get("efficiency", {})
            for key in (
                "executed_tool_calls",
                "api_requests",
                "estimated_cost_usd",
                "reported_tokens",
            ):
                if saved.get(key) != recomputed.get(key):
                    errors.append(f"{case['id']}: inconsistent {key}")
            for turn in report.get("turns", []) + report.get("reference_prefix", []):
                trace = read_jsonl(turn["trace_path"])
                starts = [e for e in trace if e["event"] == "tool_start"]
                completed = [e for e in trace if e["event"] == "tool_complete"]
                if len(starts) != len(turn.get("execution_events", [])) or len(starts) != len(
                    completed
                ):
                    errors.append(f"{case['id']}: incomplete tool callback evidence")
                if turn["sent_prompt"] != turn["prompt"]:
                    errors.append(f"{case['id']}: modified model-visible user message")
            events = read_jsonl(api_path)
            for event in events:
                if event["event"] == "api_request":
                    visible = json.dumps({k: event["body"].get(k) for k in ("messages", "tools")})
                    if "[EVAL run=" in visible or "eval_harness" in visible or "scen_" in visible:
                        errors.append(f"{case['id']}: evaluation identity visible in request")
            phases.append(
                {"id": case["id"], "api_path": api_path, "wire": wire, "metrics": recomputed}
            )
        if case.get("branch_origin") in {"sampled_prefix", "authored_reference"}:
            prefix = case.get("diagnostic_prefix") or case["shared_prefix"]
            for name, branch in case["branches"].items():
                if not branch.get("turns"):
                    continue
                trace = read_jsonl(branch["turns"][0]["trace_path"])
                start = next(e for e in trace if e["event"] == "turn_start")
                if start["input_history"] != prefix["continuation"]["history"]:
                    errors.append(f"{case['id']}/{name}: shared history differs")
    result = {
        "passed": not errors,
        "errors": errors,
        "phases": phases,
        "cost_recomputed_once_per_phase": metrics_total(
            [{"efficiency": p["metrics"]} for p in phases]
        ),
    }
    write_json_atomic(path.parent / "evidence_audit.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    result = audit_v2(parser.parse_args().report)
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "errors": result["errors"],
                "phases": len(result["phases"]),
            }
        )
    )
    sys.exit(0 if result["passed"] else 1)
