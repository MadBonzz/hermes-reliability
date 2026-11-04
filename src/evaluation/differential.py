"""Differential comparison of two benchmark runs of the same scenarios.

The baseline is the deployed system (for example an older Hermes revision); the
candidate is the changed system (a newer revision, or the same revision with an
added guard). Per scenario and condition the comparison reports pass rates and
per-tier rates (state, process, output) and classifies the change as improved,
unchanged or regressed. Runtime errors are invalid executions, not outcomes, and
are counted separately.

    uv run --locked python src/evaluation/differential.py BASE/report.json CANDIDATE/report.json \\
        --out eval_harness/differential --layers layers.json

`--layers` optionally maps scenario IDs to a property layer (framework, task,
mixed) so results can be summarised by layer.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

TIERS = ("state", "process", "output")
VALID = {"passed", "failed"}


def phase_rows(report):
    """One row per executed branch phase (ordinary, baseline or treatment)."""
    for case in report.get("cases", []) or []:
        for condition, phase in (case.get("branches") or {}).items():
            turns = phase.get("turns", [])
            yield {
                "id": case["id"],
                "condition": condition,
                "repetition": case.get("repetition"),
                "status": phase.get("status"),
                "passed": phase.get("status") == "passed",
                "failed_at_turn": phase.get("failed_at_turn"),
                "tiers": {
                    tier: bool(turns)
                    and all(t.get(f"{tier}_check", {}).get("passed") for t in turns)
                    for tier in TIERS
                },
                "phase": phase,
            }


def aggregate(rows):
    groups = defaultdict(list)
    for row in rows:
        if row["status"] in VALID:
            groups[(row["id"], row["condition"])].append(row)
    return {
        key: {
            "runs": len(items),
            "pass_rate": sum(r["passed"] for r in items) / len(items),
            "tier_rates": {t: sum(r["tiers"][t] for r in items) / len(items) for t in TIERS},
        }
        for key, items in groups.items()
    }


def report_summary(report):
    """Compact per-scenario pass rates, for indexes and prompts."""
    return {
        f"{sid}/{condition}": {"runs": v["runs"], "pass_rate": v["pass_rate"]}
        for (sid, condition), v in sorted(aggregate(phase_rows(report)).items())
    }


def verdict(base, candidate):
    if base is None or candidate is None:
        return "missing_in_" + ("baseline" if base is None else "candidate")
    if candidate["pass_rate"] > base["pass_rate"]:
        return "improved"
    if candidate["pass_rate"] < base["pass_rate"]:
        return "regressed"
    return "unchanged"


def compare(baseline_report, candidate_report, layers=None):
    layers = layers or {}
    base_rows = list(phase_rows(baseline_report))
    cand_rows = list(phase_rows(candidate_report))
    base, cand = aggregate(base_rows), aggregate(cand_rows)
    table = []
    for key in sorted(set(base) | set(cand)):
        table.append(
            {
                "id": key[0],
                "condition": key[1],
                "layer": layers.get(key[0], "unlabelled"),
                "baseline": base.get(key),
                "candidate": cand.get(key),
                "verdict": verdict(base.get(key), cand.get(key)),
            }
        )
    counts, by_layer = defaultdict(int), defaultdict(lambda: defaultdict(int))
    for row in table:
        counts[row["verdict"]] += 1
        by_layer[row["layer"]][row["verdict"]] += 1
    return {
        "verdicts": dict(counts),
        "verdicts_by_layer": {k: dict(v) for k, v in sorted(by_layer.items())},
        "table": table,
        "invalid_phases": {
            "baseline": sum(r["status"] not in VALID for r in base_rows),
            "candidate": sum(r["status"] not in VALID for r in cand_rows),
        },
    }


def _pct(x):
    return "-" if x is None else f"{x:.0%}"


def markdown(result, *, baseline_label="baseline", candidate_label="candidate"):
    lines = [
        f"# Differential comparison: {baseline_label} -> {candidate_label}",
        "",
        "Rates are over valid phases (runtime errors excluded). Tiers: state / process / output.",
        "",
        "Verdicts: "
        + (", ".join(f"{k} {v}" for k, v in sorted(result["verdicts"].items())) or "none"),
        "",
    ]
    if result["verdicts_by_layer"]:
        lines += ["| Layer | Verdicts |", "|---|---|"]
        for layer, counts in result["verdicts_by_layer"].items():
            lines.append(
                f"| {layer} | " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + " |"
            )
        lines.append("")
    lines += [
        f"| Scenario | Condition | Layer | Runs | Pass {baseline_label} | Pass {candidate_label} "
        f"| Tiers {baseline_label} | Tiers {candidate_label} | Verdict |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    def tiers(x):
        rates = (x or {}).get("tier_rates") or {}
        return "/".join(_pct(rates.get(t)) for t in TIERS) if rates else "-"

    for row in result["table"]:
        b, c = row["baseline"] or {}, row["candidate"] or {}
        lines.append(
            f"| {row['id']} | {row['condition']} | {row['layer']} | "
            f"{b.get('runs', 0)}/{c.get('runs', 0)} | {_pct(b.get('pass_rate'))} | "
            f"{_pct(c.get('pass_rate'))} | {tiers(b)} | {tiers(c)} | {row['verdict']} |"
        )
    lines += [
        "",
        f"Invalid phases excluded: {baseline_label} {result['invalid_phases']['baseline']}, "
        f"{candidate_label} {result['invalid_phases']['candidate']}.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    parser.add_argument("--out")
    parser.add_argument("--layers", help="JSON object mapping scenario IDs to property layers")
    args = parser.parse_args(argv)
    layers = json.loads(Path(args.layers).read_text(encoding="utf-8")) if args.layers else None
    result = compare(
        json.loads(Path(args.baseline).read_text(encoding="utf-8")),
        json.loads(Path(args.candidate).read_text(encoding="utf-8")),
        layers,
    )
    text = markdown(result)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "differential.json").write_text(json.dumps(result, indent=2) + "\n", "utf-8")
        (out / "differential.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
