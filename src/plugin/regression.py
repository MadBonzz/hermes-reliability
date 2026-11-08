"""Execute a frozen benchmark without a generation or evaluator-editing step."""

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ..evaluation.api_trace import SecretRedactor
from ..evaluation.audit import audit_campaign
from ..evaluation.runtime_support import write_json_atomic
from ..orchestrator.adapters import make_adapter, revision_hashes
from ..orchestrator.runner import CampaignPreempted, check_stop, prepare_source, target_api_usage
from .history import repository, tree_hashes


def run_fixed(config, history, *, on_created, credential_values, adapter_factory=make_adapter):
    selection = config["regression"]
    manifest, frozen = history.benchmark(selection["benchmark_id"])
    baseline = history.get_run(selection["baseline_run_id"], verify=True)
    if (
        baseline["benchmark_id"] != manifest["id"]
        or baseline["lane"] != "trusted"
        or config["adapter"]["kind"] != manifest["runtime"]
        or repository(config["source"]["repository"]) != manifest["repository"]
    ):
        raise ValueError("Benchmark, baseline, repository and runtime must match")
    stamp, attempt = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"), uuid.uuid4().hex
    output = (
        Path(config["artifacts_root"]) / f"regression_{stamp}_{attempt[:8]}" / "attempts" / attempt
    )
    output.mkdir(parents=True)
    redactor = SecretRedactor(credential_values.values())
    state = {"mode": "regression", "rounds": [], "stop_reason": "error"}

    def save(name, value):
        write_json_atomic(output / name, redactor(value))

    def progress(phase, **details):
        check_stop(output)
        save(
            "progress.json",
            {"phase": phase, "updated_at": datetime.now(UTC).isoformat(), **details},
        )
        save("campaign.json", state)

    save("config.json", config)
    on_created(output)
    try:
        progress("source")
        source = prepare_source(config, output)
        if factory := config["adapter"].get("factory"):
            filename, _, function = factory.rpartition(":")
            destination = output / "adapter/factory.py"
            destination.parent.mkdir()
            shutil.copyfile(filename, destination)
            config["adapter"].update(
                factory=str(destination) + ":" + function, factory_origin=filename
            )
        save("config.json", config)
        # These provenance records describe where the frozen test design came from;
        # this run neither samples new workloads nor claims fresh design reviews.
        for name in manifest.get("provenance", {}):
            destination = output / "benchmark_provenance" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(frozen.parent / "provenance" / name, destination)
            if name == "datasets/manifest.json":
                (output / "datasets").mkdir()
                shutil.copyfile(destination, output / name)
        save("benchmark.json", manifest)
        shutil.copytree(frozen, output / "working")
        adapter = adapter_factory(
            {
                **config["adapter"],
                "hermes_root": str(source),
                "source_root": str(source),
                "source_revision": config["source"].get("revision"),
                "target": config["target"],
                "design_only": False,
            }
        )
        adapter.schema_path = frozen / "scenario.schema.json"
        adapter.schema = json.loads(adapter.schema_path.read_text())
        record = {"round": 1, "generations": [], "executions": {}}
        state["rounds"].append(record)
        # No candidate edits exist in this mode. The frozen trusted evaluator is
        # the sole measurement lane, compared with an audited historical run.
        lane = output / "round_001/trusted"
        lane.mkdir(parents=True)
        shutil.copytree(frozen, lane / "evaluator")
        progress("execution", round=1, lane="trusted")
        save("round_001/trusted/execution_input_hashes.json", revision_hashes(lane / "evaluator"))
        result = adapter.execute_revision(
            lane / "evaluator",
            lane / "execution",
            config["target"],
            credential_values.get("RELIABILITY_TARGET_API_KEY", ""),
        )
        unchanged = tree_hashes(lane / "evaluator") == manifest["files"]
        result["revision_unchanged"] = unchanged and result.get("revision_unchanged", True)
        if not result["revision_unchanged"]:
            result["status"] = "invalid_evaluator"
        report = lane / "execution/report.json"
        if report.exists():
            try:
                audit = audit_campaign(report)
                result["evidence_audit"] = {"passed": audit["passed"], "errors": audit["errors"]}
            except (ValueError, OSError, KeyError, TypeError) as exc:
                result["evidence_audit"] = {"passed": False, "error": str(exc)}
        save("round_001/trusted/execution_result.json", result)
        record["executions"]["trusted"] = result
        check_stop(output)
        history.benchmark(manifest["id"])
        # The record is indexed before comparison, with the same validation as
        # exploration results. Rechecking the baseline catches concurrent edits.
        save("campaign.json", state)
        current = history.record_campaign_for_comparison(output)[0]
        comparison = history.comparison(baseline["id"], current["id"])
        save("regression_comparison.json", comparison)
        state["stop_reason"] = (
            "regression_complete" if comparison["status"] == "comparable" else comparison["status"]
        )
    except CampaignPreempted:
        state.update(
            stop_reason="operator_preempted", preemption_reason="Operator requested a stop"
        )
    except Exception as exc:
        state.update(stop_reason="error", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        save("campaign.json", state)
        save(
            "progress.json",
            {
                "phase": "terminal",
                "stop_reason": state["stop_reason"],
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        save(
            "orchestrator_usage.json",
            {
                "requests": 0,
                "reported_tokens": {"input_tokens": 0, "output_tokens": 0},
                "target_api_usage": target_api_usage(output),
            },
        )
    return output
