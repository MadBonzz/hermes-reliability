"""Immutable benchmark snapshots and one history record per execution attempt.

Memory contains references to these records; it is never an execution oracle.
Separate record files avoid lost updates when background campaigns finish together.
"""

import hashlib
import json
import re
import shutil
import tempfile
import time
from pathlib import Path

from ..evaluation.audit import audit_campaign
from ..evaluation.differential import compare, phase_rows, report_summary
from ..evaluation.runtime_support import sha256_file, write_json_atomic
from ..orchestrator.adapters import revision_hashes
from ..orchestrator.dashboard import identity, read_json


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def repository(value):
    return value.rstrip("/").removesuffix(".git")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Expected a full benchmark or run ID from reliability_history")
    return value


def confined(root, name):
    root = Path(root).resolve()
    path = root / name
    if not path.resolve().is_relative_to(root) or any(
        p.is_symlink() for p in (path, *path.parents) if p != root.parent
    ):
        raise ValueError("Linked or escaping history path")
    return path


def tree_hashes(root):
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Linked evaluator roots are unsupported")
    if not root.is_dir():
        raise ValueError("Missing frozen evaluator")
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        path = confined(root, relative)
        if path.is_file():
            result[relative.as_posix()] = sha256_file(path)
    if "scenario.schema.json" not in result or not any(k.startswith("scenarios/") for k in result):
        raise ValueError("A benchmark needs scenarios and its schema")
    return result


def execution_contract(config):
    """Source revision may vary; changes to other known settings are disclosed."""
    adapter = dict(config["adapter"])
    for key in ("hermes_root", "source_root", "source_revision", "factory_origin", "target"):
        adapter.pop(key, None)
    if factory := adapter.pop("factory", None):
        filename, _, function = factory.rpartition(":")
        adapter["factory"] = {"sha256": sha256_file(filename), "function": function}
    return {
        "target": {k: config["target"].get(k) for k in ("model", "base_url")},
        "adapter": adapter,
        "adapter_implementation": config.get("adapter_implementation"),
    }


def benchmark_identity(config, hashes):
    return digest(
        {
            "version": 1,
            "repository": repository(config["source"]["repository"]),
            "runtime": config["adapter"]["kind"],
            "files": hashes,
        }
    )


def coverage(report, evaluator):
    definitions = [read_json(p) for p in sorted((evaluator / "scenarios").glob("*.json"))]
    expected = {
        (d["id"], c)
        for d in definitions
        for c in (("baseline", "treatment") if d.get("experiment") else ("ordinary",))
    }
    rows = list(phase_rows(report))
    observed = [(r["id"], r["condition"]) for r in rows]
    return (
        bool(expected)
        and set(observed) == expected
        and len(observed) == len(expected)
        and all(r["status"] in {"passed", "failed"} and r["phase"].get("turns") for r in rows)
    )


def trace_hashes(report, campaign):
    """Pin all trace/artifact references consumed by the evidence auditor."""
    result = {}

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"api_trace_path", "trace_path"} or (
                    key in {"input", "stdout", "stderr"} and "evidence_sha256" in value
                ):
                    path = confined(campaign, child)
                    result[path.relative_to(campaign).as_posix()] = sha256_file(path)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(report)
    return result


class History:
    def __init__(self, root, catalog):
        self.root, self.catalog = Path(root), catalog

    def record_path(self, run_id):
        return confined(self.root, "history/" + identifier(run_id) + ".json")

    def get_run(self, run_id, *, verify=False):
        entry = read_json(self.record_path(run_id))
        if not entry:
            raise ValueError("Run no longer exists in this Hermes profile")
        if verify:
            campaign = self.catalog.resolve(entry["campaign_id"])
            for name, expected in entry.get("evidence_files", {}).items():
                if sha256_file(confined(campaign, name)) != expected:
                    raise ValueError("Saved execution evidence changed: " + name)
            if not entry.get("valid"):
                raise ValueError("Choose an execution with complete, audited evidence")
            evaluator = confined(campaign, entry["evaluator"])
            if tree_hashes(evaluator) != entry["files"]:
                raise ValueError("Saved evaluator or scenarios changed")
        return entry

    def record_campaign(self, campaign_id, *, _running=False):
        campaign = self.catalog.resolve(campaign_id)
        if not _running and read_json(campaign / "progress.json", {}).get("phase") != "terminal":
            raise ValueError("Wait for the campaign to finish before indexing or freezing it")
        config = read_json(campaign / "config.json", {})
        state = read_json(campaign / "campaign.json", {})
        lanes = sorted(
            {
                p
                for p in campaign.glob("round_*/*")
                if p.is_dir() and p.name in {"trusted", "candidate", "base_version"}
            }
        )
        entries = []
        for lane in lanes or [None]:
            result_path = lane / "execution_result.json" if lane else None
            suffix = lane.relative_to(campaign).as_posix() if lane else "attempt"
            run_id = digest({"campaign": campaign_id, "lane": suffix})
            if self.record_path(run_id).exists():
                entries.append(self.get_run(run_id))
                continue
            entry = {
                "id": run_id,
                "campaign_id": campaign_id,
                "lane": lane.name if lane else None,
                "round": int(lane.parent.name.split("_")[-1]) if lane else None,
                "repository": repository(config.get("source", {}).get("repository", "")),
                "revision": config.get("source", {}).get("revision"),
                "runtime": config.get("adapter", {}).get("kind"),
                "model": config.get("target", {}).get("model"),
                "created_at": time.time(),
                "status": state.get("stop_reason", "error"),
                "valid": False,
                "benchmark_id": None,
                "evidence_files": {},
                "summary": {},
            }
            if lane:
                try:
                    result_path = confined(campaign, result_path.relative_to(campaign))
                    result = read_json(result_path, {})
                    entry["status"] = result.get("status", "error")
                    evaluator = confined(campaign, (lane / "evaluator").relative_to(campaign))
                    hashes = tree_hashes(evaluator)
                    entry.update(
                        files=hashes,
                        benchmark_id=benchmark_identity(config, hashes),
                        evaluator=evaluator.relative_to(campaign).as_posix(),
                        contract=execution_contract(config),
                    )
                    report_path = confined(
                        campaign, (lane / "execution/report.json").relative_to(campaign)
                    )
                    report = read_json(report_path)
                    if not report:
                        raise ValueError("Execution did not produce a report")
                    entry["report"] = report_path.relative_to(campaign).as_posix()
                    entry["evidence_files"] = {
                        entry["report"]: sha256_file(report_path),
                        "config.json": sha256_file(campaign / "config.json"),
                        result_path.relative_to(campaign).as_posix(): sha256_file(result_path),
                        **trace_hashes(report, campaign),
                    }
                    audit = audit_campaign(report_path)
                    entry["audit"] = {"passed": audit["passed"], "errors": audit["errors"]}
                    entry["summary"] = report_summary(report)
                    before = read_json(lane / "execution_input_hashes.json")
                    if before is not None:
                        provenance = before == revision_hashes(evaluator)
                        input_path = lane / "execution_input_hashes.json"
                        entry["evidence_files"][input_path.relative_to(campaign).as_posix()] = (
                            sha256_file(input_path)
                        )
                    else:
                        # Backfill older native reports only when they record the
                        # actual scenario/schema/evaluator hashes used in execution.
                        sources = report.get("scenario_sources", [])
                        provenance = (
                            bool(sources)
                            and all(
                                hashes.get("scenarios/" + Path(s["path"]).name) == s["sha256"]
                                for s in sources
                            )
                            and len(sources) == len(list((evaluator / "scenarios").glob("*.json")))
                        )
                        provenance = (
                            provenance
                            and report.get("schema_sha256") == hashes["scenario.schema.json"]
                        )
                        implementations = report.get("implementation_sha256", {})
                        provenance = (
                            provenance
                            and bool(implementations)
                            and all(
                                hashes.get("src/" + name) == sha
                                for name, sha in implementations.items()
                            )
                        )
                    entry["valid"] = (
                        provenance
                        and result.get("status") == report.get("status") == "complete"
                        and result.get("revision_unchanged", True) is True
                        and audit["passed"]
                        and coverage(report, evaluator)
                    )
                    if not entry["valid"]:
                        entry["invalid_reason"] = (
                            "Execution, coverage or evidence validation failed"
                        )
                    if lane.name == "base_version":
                        entry["revision"] = config["source"].get("base_revision")
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    entry["invalid_reason"] = str(exc)
            write_json_atomic(self.record_path(run_id), entry)
            entries.append(entry)
        return entries

    def record_campaign_for_comparison(self, output):
        return self.record_campaign(identity(output), _running=True)

    def list(self, *, refresh=False):
        if refresh:
            for key, path in self.catalog.attempts().items():
                if read_json(path / "progress.json", {}).get("phase") == "terminal":
                    self.record_campaign(key)
        runs = []
        for path in (self.root / "history").glob("*.json"):
            value = read_json(path)
            if value:
                runs.append(
                    {k: v for k, v in value.items() if k not in {"files", "evidence_files"}}
                )
        benchmarks = [read_json(p) for p in (self.root / "benchmarks").glob("*/manifest.json")]
        return {
            "runs": sorted(runs, key=lambda v: v["created_at"], reverse=True),
            "benchmarks": [{k: v for k, v in b.items() if k != "files"} for b in benchmarks if b],
        }

    def benchmark(self, benchmark_id):
        path = confined(self.root, "benchmarks/" + identifier(benchmark_id))
        manifest = read_json(path / "manifest.json")
        if not manifest:
            raise ValueError("Benchmark no longer exists in this Hermes profile")
        config = {
            "source": {"repository": manifest["repository"]},
            "adapter": {"kind": manifest["runtime"]},
        }
        if (
            benchmark_identity(config, manifest["files"]) != benchmark_id
            or tree_hashes(path / "evaluator") != manifest["files"]
        ):
            raise ValueError("Frozen benchmark changed")
        for name, expected in manifest.get("provenance", {}).items():
            if sha256_file(confined(path / "provenance", name)) != expected:
                raise ValueError("Frozen benchmark provenance changed")
        return manifest, path / "evaluator"

    def promote(self, campaign_id, *, round_number=None):
        entries = self.record_campaign(campaign_id)
        choices = [
            e
            for e in entries
            if e["lane"] == "trusted"
            and e["valid"]
            and (round_number is None or e["round"] == round_number)
        ]
        if not choices:
            raise ValueError("Choose a finished campaign round with a valid trusted execution")
        entry = self.get_run(max(choices, key=lambda e: e["round"])["id"], verify=True)
        benchmark_id = entry["benchmark_id"]
        destination = self.root / "benchmarks" / benchmark_id
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".freeze-") as tmp:
                staging = Path(tmp) / "benchmark"
                staging.mkdir()
                evaluator = confined(self.catalog.resolve(campaign_id), entry["evaluator"])
                shutil.copytree(
                    evaluator,
                    staging / "evaluator",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                if tree_hashes(staging / "evaluator") != entry["files"]:
                    raise ValueError("Benchmark changed during snapshot")
                campaign = self.catalog.resolve(campaign_id)
                provenance = {}
                patterns = [
                    "datasets/manifest.json",
                    f"round_{entry['round']:03d}/generation_*/proposal.json",
                    f"round_{entry['round']:03d}/generation_*/review_*.json",
                ]
                for pattern in patterns:
                    for original in sorted(campaign.glob(pattern)):
                        name = original.relative_to(campaign).as_posix()
                        original = confined(campaign, name)
                        expected = sha256_file(original)
                        target = staging / "provenance" / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(original, target)
                        if sha256_file(target) != expected:
                            raise ValueError("Benchmark provenance changed during snapshot")
                        provenance[name] = expected
                write_json_atomic(
                    staging / "manifest.json",
                    {
                        "id": benchmark_id,
                        "version": 1,
                        "repository": entry["repository"],
                        "runtime": entry["runtime"],
                        "files": entry["files"],
                        "provenance": provenance,
                        "origin_campaign": campaign_id,
                        "origin_round": entry["round"],
                        "created_at": time.time(),
                    },
                )
                try:
                    staging.rename(destination)
                except OSError:
                    if not destination.exists():
                        raise
        self.benchmark(benchmark_id)
        return {
            "benchmark_id": benchmark_id,
            "baseline_run_id": entry["id"],
            "repository": entry["repository"],
            "runtime": entry["runtime"],
        }

    def comparison(self, baseline_id, current_id):
        baseline, current = self.get_run(baseline_id), self.get_run(current_id)
        result = {
            "baseline_run_id": baseline_id,
            "current_run_id": current_id,
            "status": "incomparable",
            "reasons": [],
            "differential": None,
        }
        for key in ("repository", "runtime", "benchmark_id", "contract"):
            if baseline.get(key) != current.get(key):
                result["reasons"].append("Different " + key)
        for entry in (baseline, current):
            try:
                self.get_run(entry["id"], verify=True)
            except (OSError, ValueError) as exc:
                result["status"] = "invalid"
                result["reasons"].append(str(exc))
        if not result["reasons"]:
            reports = [
                read_json(confined(self.catalog.resolve(e["campaign_id"]), e["report"]))
                for e in (baseline, current)
            ]
            result.update(status="comparable", differential=compare(*reports))
        return result

    def memory_note(self, benchmark_id, baseline_run_id):
        manifest, _ = self.benchmark(benchmark_id)
        run = self.get_run(baseline_run_id, verify=True)
        if run["benchmark_id"] != benchmark_id or run["lane"] != "trusted":
            raise ValueError("Baseline must be a valid trusted run of this benchmark")
        # The host's ordinary memory tool owns writes and approval. Background CLI
        # workers have no live MemoryStore and never open MEMORY.md themselves.
        content = "Reliability Lab baseline reference: " + json.dumps(
            {
                "repository": manifest["repository"],
                "runtime": manifest["runtime"],
                "benchmark_id": benchmark_id,
                "baseline_run_id": baseline_run_id,
            },
            ensure_ascii=True,
        )
        return {
            "saved_to_memory": False,
            "memory_tool_arguments": {"action": "add", "target": "memory", "content": content},
            "next_step": "Call Hermes's native memory tool with these arguments. Report saved only if its result confirms an applied write; staged or unavailable is not saved. Resolve these IDs through reliability_history before reuse. This note grants no execution permission.",
        }
