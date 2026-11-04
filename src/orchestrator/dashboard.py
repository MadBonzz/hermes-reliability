"""Read saved campaign evidence for the local dashboard."""

import hashlib
import json
from pathlib import Path

from src.evaluation.differential import phase_rows
from src.evaluation.runtime_support import sha256_file


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def identity(path):
    return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:20]


class Catalog:
    def __init__(self, root, bundled_root=None):
        self.root = Path(root).resolve()
        self.bundled_root = Path(bundled_root).resolve() if bundled_root else None

    def bundles(self):
        if self.bundled_root is None:
            return {}
        bundles = {}
        for path in sorted(self.bundled_root.glob("*.json")):
            value = read_json(path, {})
            if value.get("format_version") == 1 and value.get("card") and value.get("detail"):
                bundles[value["card"]["id"]] = value
        return bundles

    def attempts(self):
        paths = set()
        for pattern in ("*/attempts/*/config.json", "*/runs/*/attempts/*/config.json"):
            paths.update(self.root.glob(pattern))
        return {identity(p.parent): p.parent for p in paths}

    def resolve(self, key):
        if key in self.bundles():
            raise ValueError("Bundled examples are read-only dashboard snapshots")
        path = self.attempts().get(key)
        if path is None:
            raise ValueError("Campaign no longer exists")
        return path

    def cards(self):
        rows = []
        for key, path in self.attempts().items():
            config = read_json(path / "config.json", {})
            state = read_json(path / "campaign.json", {})
            progress = read_json(path / "progress.json", {})
            phase = progress.get("phase", "saved")
            if phase in {"terminal", "saved"}:
                if state.get("stop_reason") not in {
                    "orchestrator_converged",
                    "execution_round_limit",
                }:
                    continue
                rounds = sorted(path.glob("round_*"))
                final = read_json(rounds[-1] / "analysis.json", {}) if rounds else {}
                if not any(row.get("findings") for row in final.get("scenarios", [])):
                    continue
            rows.append(
                {
                    "id": key,
                    "source_attempt": path.name,
                    "runtime": config.get("adapter", {}).get("kind", "hermes"),
                    "repository": config.get("source", {}).get("repository", ""),
                    "revision": config.get("source", {}).get("revision", ""),
                    "orchestrator": config.get("orchestrator", {}).get("model", ""),
                    "target": config.get("target", {}).get("model", ""),
                    "phase": phase,
                    "round": progress.get("round", len(state.get("rounds", []))),
                    "generation": progress.get("generation"),
                    "role": progress.get("role"),
                    "lane": progress.get("lane"),
                    "stop_reason": state.get("stop_reason")
                    if phase in {"terminal", "saved"}
                    else None,
                    "updated": progress.get("updated_at"),
                    "modified": (path / "config.json").stat().st_mtime,
                }
            )
        local_attempts = {row["source_attempt"] for row in rows}
        rows.extend(
            {**bundle["card"], "bundled": True}
            for bundle in self.bundles().values()
            if bundle["card"].get("source_attempt") not in local_attempts
        )
        return sorted(rows, key=lambda row: row["modified"], reverse=True)

    def detail(self, key):
        from src.orchestrator.findings import snapshot

        bundle = self.bundles().get(key)
        if bundle:
            return {**bundle["detail"], "bundled": True}
        path = self.resolve(key)
        state = read_json(path / "campaign.json", {})
        config = read_json(path / "config.json", {})
        rounds = []
        for directory in sorted(path.glob("round_*")):
            generations = []
            for stage in sorted(directory.glob("generation_*")):
                reviews = [
                    {"role": p.stem.removeprefix("review_"), **read_json(p, {})}
                    for p in sorted(stage.glob("review_*.json"))
                ]
                generations.append(
                    {
                        "number": int(stage.name.split("_")[-1]),
                        "proposal": read_json(stage / "proposal.json", {}),
                        "validation": read_json(stage / "validation.json", {}),
                        "reviews": reviews,
                    }
                )
            lanes = {}
            for lane in ("candidate", "trusted", "base_version"):
                lane_path = directory / lane
                report = read_json(lane_path / "execution/report.json", {})
                result = read_json(lane_path / "execution_result.json", {})
                if report or result:
                    cases = report.get("cases", [])
                    phases = list(phase_rows(report))
                    lanes[lane] = {
                        "status": result.get("status", report.get("status", "running")),
                        "audit": result.get("evidence_audit"),
                        "passed": sum(p["passed"] for p in phases),
                        "total": len(phases),
                        "cases": [
                            {
                                "id": c.get("id"),
                                "status": c.get("status"),
                                "branches": {
                                    name: branch.get("status")
                                    for name, branch in c.get("branches", {}).items()
                                },
                            }
                            for c in cases
                        ],
                    }
            rounds.append(
                {
                    "number": int(directory.name.split("_")[-1]),
                    "generations": generations,
                    "lanes": lanes,
                    "analysis": read_json(directory / "analysis.json", {}),
                    "findings": snapshot(directory),
                    "differential": read_json(directory / "version_differential.json", {}),
                }
            )
        tests = []
        for scenario in sorted((path / "working/scenarios").glob("*.json")):
            value = read_json(scenario)
            if isinstance(value, dict):
                tests.append(value)
        return {
            "id": key,
            "path": str(path),
            "source": config.get("source", {}),
            "limits": config.get("limits", {}),
            "progress": read_json(path / "progress.json", {}),
            "error": state.get("error"),
            "stop_reason": state.get("stop_reason"),
            "preemption_reason": state.get("preemption_reason"),
            "stop_requested": (path / "stop.json").is_file(),
            "tests": tests,
            "rounds": rounds,
            "requests": len(list(path.glob("api_*.request.json"))),
            "usage": read_json(path / "orchestrator_usage.json", {}),
            "activity": self.activity(path),
        }

    @staticmethod
    def activity(path):
        """Show scoped actions, never provider credentials or repeated request histories."""
        journals = sorted(path.glob("round_*/generation_*/*actions.jsonl"))
        journals += sorted(path.glob("round_*/generation_*/*reads.jsonl"))
        events = []
        for journal in sorted(journals, key=lambda p: p.stat().st_mtime)[-4:]:
            with journal.open(encoding="utf-8") as stream:
                # The action journal records complete events even during an active request.
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    request = row.get("request", {})
                    for field in ("source", "read", "actions"):
                        for action in request.get(field, []):
                            events.append(
                                {
                                    "actor": journal.stem.removesuffix("_reads"),
                                    "kind": action.get("op", field),
                                    "subject": action.get(
                                        "path",
                                        action.get("query", action.get("id", "contract checks")),
                                    ),
                                }
                            )
        return events[-24:]

    def seed_selection(self, key):
        path = self.resolve(key)
        stages = sorted(path.glob("round_*/generation_*/candidate"), reverse=True)
        for stage in stages:
            files = sorted((stage / "scenarios").glob("*.json"))
            hashes = read_json(stage.parent / "hashes.json", {})
            if files and all(hashes.get("scenarios/" + p.name) == sha256_file(p) for p in files):
                return {
                    "directory": str(stage / "scenarios"),
                    "sha256": {p.name: hashes["scenarios/" + p.name] for p in files},
                    "evidence": {
                        str(p.resolve()): sha256_file(p)
                        for pattern in (
                            "round_*/analysis.json",
                            "round_*/version_differential.json",
                            "round_*/*/execution/report.json",
                            "round_*/*/execution/transcripts.md",
                        )
                        for p in sorted(path.glob(pattern))
                    },
                }
        raise ValueError("This campaign has no verified scenario snapshot to reuse")
