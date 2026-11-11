"""Frozen-suite lifecycle and rejection tests; model and target outcomes are fixtures."""

import copy
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from src.evaluation.runtime_support import sha256_file, write_json_atomic
from src.orchestrator.adapters import revision_hashes
from src.orchestrator.dashboard import identity, read_json
from src.orchestrator.source import snapshot_directory
from src.orchestrator.workspace import Workspace
from src.plugin.history import History, tree_hashes
from src.plugin.integration import register
from src.plugin.regression import run_fixed
from src.plugin.service import Service
from tests.helpers import TemporaryDirectoryTestCase
from tests.host_safety import HostSafetyMixin
from tests.test_orchestrator import ROOT, case
from tests.test_version_regression import report


class RegressionTests(HostSafetyMixin, TemporaryDirectoryTestCase):
    def setUp(self):
        super().setUp()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "agent.py").write_text("print('fixture')\n")
        self.factory = self.root / "factory.py"
        self.factory.write_text("def create_adapter(config): pass\n")
        self.service = Service(
            self.root / "profile",
            options={
                "execution_enabled": True,
                "target": {"model": "fixture", "base_url": "http://localhost:1234/v1"},
                "runtimes": {"custom": {"factory": str(self.factory) + ":create_adapter"}},
            },
        )
        self.history = self.service.history
        self.audit = patch(
            "src.plugin.history.audit_campaign", return_value={"passed": True, "errors": []}
        )
        self.audit.start()
        self.addCleanup(self.audit.stop)
        self.audit_fixed = patch(
            "src.plugin.regression.audit_campaign", return_value={"passed": True, "errors": []}
        )
        self.audit_fixed.start()
        self.addCleanup(self.audit_fixed.stop)

    def campaign(self, name="one", passed=True, *, model="fixture", definitions=None):
        folder = self.service.catalog.root / name / "attempts" / name
        lane = folder / "round_001/trusted"
        lane.mkdir(parents=True)
        ws = Workspace(lane / "evaluator", ROOT)
        write_json_atomic(ws.root / "scenarios/task_a.json", definitions or case())
        config = self.service.validate(
            {"source": str(self.source), "runtime": "custom", "execute": True}
        )
        config["source"] = snapshot_directory(self.source, self.root / (name + "_snapshot"))
        config["target"]["model"] = model
        write_json_atomic(folder / "config.json", config)
        write_json_atomic(folder / "campaign.json", {"stop_reason": "max_rounds"})
        write_json_atomic(folder / "progress.json", {"phase": "terminal"})
        self.result(lane, passed)
        return identity(folder), folder, config

    def result(self, lane, passed=True):
        write_json_atomic(lane / "execution_input_hashes.json", revision_hashes(lane / "evaluator"))
        write_json_atomic(
            lane / "execution/report.json", {"status": "complete", **report({"task_a": [passed]})}
        )
        write_json_atomic(
            lane / "execution_result.json", {"status": "complete", "revision_unchanged": True}
        )

    def selection(self):
        cid, folder, config = self.campaign()
        return self.history.promote(cid), folder, config

    def test_freeze_is_idempotent_and_new_assertions_create_new_version(self):
        first, folder, _ = self.selection()
        self.assertEqual(first, self.history.promote(identity(folder)))
        changed = case()
        changed["turns"][0]["output_checks"][0]["values"] = ["four"]
        cid, _, _ = self.campaign("changed", definitions=changed)
        second = self.history.promote(cid)
        self.assertNotEqual(first["benchmark_id"], second["benchmark_id"])
        _, old = self.history.benchmark(first["benchmark_id"])
        self.assertEqual(case(), read_json(old / "scenarios/task_a.json"))
        self.assertEqual(2, len(self.history.list()["benchmarks"]))

    def test_dataset_and_review_provenance_are_frozen_and_verified(self):
        cid, folder, _ = self.campaign()
        manifest = {"sources": [{"id": "fixture", "sample_status": "metadata_only"}]}
        write_json_atomic(folder / "datasets/manifest.json", manifest)
        review = "round_001/generation_01/review_task_quality.json"
        write_json_atomic(folder / review, {"accepted": False, "issues": ["fixture concern"]})
        result = self.history.promote(cid)
        frozen, evaluator = self.history.benchmark(result["benchmark_id"])
        write_json_atomic(folder / "datasets/manifest.json", {"changed": True})
        self.assertEqual(
            manifest, read_json(evaluator.parent / "provenance/datasets/manifest.json")
        )
        self.assertIn(review, frozen["provenance"])
        (evaluator.parent / "provenance" / review).write_text("{}")
        with self.assertRaisesRegex(ValueError, "provenance changed"):
            self.history.benchmark(result["benchmark_id"])

    def test_frozen_execution_has_no_generator_and_compares_regression(self):
        cid, original, config = self.campaign()
        provenance = {"sources": [{"id": "fixture", "sample_status": "metadata_only"}]}
        write_json_atomic(original / "datasets/manifest.json", provenance)
        selection = self.history.promote(cid)
        write_json_atomic(original / "datasets/manifest.json", {"changed_after_freeze": True})
        config.update(
            mode="regression",
            regression={k: selection[k] for k in ("benchmark_id", "baseline_run_id")},
        )
        expected, _ = self.history.benchmark(selection["benchmark_id"])
        calls = []
        owner = self

        class Adapter:
            def execute_revision(self, evaluator, output, target, key):
                calls.append(tree_hashes(evaluator))
                owner.result(output.parent, False)
                return {"status": "complete", "revision_unchanged": True}

        with patch(
            "src.plugin.client.HermesClient.ask",
            side_effect=AssertionError("No generation allowed"),
        ):
            output = run_fixed(
                config,
                self.history,
                on_created=lambda p: None,
                credential_values={},
                adapter_factory=lambda c: Adapter(),
            )
        self.assertEqual([expected["files"]], calls)
        result = read_json(output / "regression_comparison.json")
        self.assertEqual("comparable", result["status"])
        self.assertEqual({"regressed": 1}, result["differential"]["verdicts"])
        self.assertEqual(0, read_json(output / "orchestrator_usage.json")["requests"])
        self.assertFalse(list(output.glob("round_*/generation_*")))
        self.assertEqual(provenance, read_json(output / "datasets/manifest.json"))
        self.assertEqual("regression_complete", read_json(output / "campaign.json")["stop_reason"])

    def test_changed_configuration_is_incomparable_not_regression(self):
        first, _, _ = self.selection()
        cid, _, _ = self.campaign("new_model", False, model="different")
        current = self.history.record_campaign(cid)[0]
        result = self.history.comparison(first["baseline_run_id"], current["id"])
        self.assertEqual("incomparable", result["status"])
        self.assertIn("Different contract", result["reasons"])
        self.assertIsNone(result["differential"])

    def test_tampered_evidence_and_evaluator_cannot_be_baselines(self):
        first, folder, _ = self.selection()
        report_path = folder / "round_001/trusted/execution/report.json"
        original = report_path.read_text()
        report_path.write_text(original + " ")
        result = self.history.comparison(first["baseline_run_id"], first["baseline_run_id"])
        self.assertEqual("invalid", result["status"])
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            self.history.memory_note(first["benchmark_id"], first["baseline_run_id"])
        report_path.write_text(original)
        _, evaluator = self.history.benchmark(first["benchmark_id"])
        (evaluator / "scenarios/task_a.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "benchmark changed"):
            self.history.benchmark(first["benchmark_id"])

    def test_audit_failure_missing_coverage_and_preexecution_tampering_remain_invalid(self):
        for reason in ("audit", "coverage", "empty_turns", "tamper", "missing_report"):
            with self.subTest(reason=reason):
                cid, folder, _ = self.campaign(reason)
                lane = folder / "round_001/trusted"
                if reason == "coverage":
                    write_json_atomic(
                        lane / "execution/report.json", {"status": "complete", "cases": []}
                    )
                if reason == "empty_turns":
                    value = read_json(lane / "execution/report.json")
                    value["cases"][0]["branches"]["ordinary"]["turns"] = []
                    write_json_atomic(lane / "execution/report.json", value)
                if reason == "tamper":
                    (lane / "evaluator/scenarios/task_a.json").write_text(json.dumps(case()) + " ")
                if reason == "missing_report":
                    (lane / "execution/report.json").unlink()
                with patch(
                    "src.plugin.history.audit_campaign",
                    return_value={"passed": reason != "audit", "errors": []},
                ):
                    entry = self.history.record_campaign(cid)[0]
                self.assertFalse(entry["valid"])
                with self.assertRaisesRegex(ValueError, "valid trusted"):
                    self.history.promote(cid)
        self.assertEqual(5, len(self.history.list()["runs"]))

    def test_history_backfill_and_concurrent_completions_do_not_lose_records(self):
        ids = [self.campaign(str(i))[0] for i in range(3)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(self.history.record_campaign, ids))
        self.history.list(refresh=True)
        self.assertEqual(3, len(self.history.list()["runs"]))

    def test_legacy_native_reports_need_matching_scenario_schema_and_code_hashes(self):
        for name, corrupt in (("legacy_ok", False), ("legacy_bad", True)):
            cid, folder, _ = self.campaign(name)
            lane = folder / "round_001/trusted"
            (lane / "execution_input_hashes.json").unlink()
            evaluator = lane / "evaluator"
            value = read_json(lane / "execution/report.json")
            value.update(
                scenario_sources=[
                    {
                        "path": str(evaluator / "scenarios/task_a.json"),
                        "sha256": sha256_file(evaluator / "scenarios/task_a.json"),
                    }
                ],
                schema_sha256="wrong"
                if corrupt
                else sha256_file(evaluator / "scenario.schema.json"),
                implementation_sha256={
                    p.relative_to(evaluator / "src").as_posix(): sha256_file(p)
                    for p in (evaluator / "src").rglob("*.py")
                },
            )
            write_json_atomic(lane / "execution/report.json", value)
            entry = self.history.record_campaign(cid)[0]
            self.assertEqual(not corrupt, entry["valid"])
            if not corrupt:
                self.assertEqual(entry["benchmark_id"], self.history.promote(cid)["benchmark_id"])

    def test_reject_active_campaign_and_cross_profile_ids(self):
        selection, folder, _ = self.selection()
        write_json_atomic(folder / "progress.json", {"phase": "execution"})
        with self.assertRaisesRegex(ValueError, "finish"):
            self.history.promote(identity(folder))
        other = History(self.root / "other", self.service.catalog)
        with self.assertRaisesRegex(ValueError, "profile"):
            other.get_run(selection["baseline_run_id"])
        for bad in ("../config", "a" * 12, None):
            with self.assertRaises(ValueError):
                self.history.benchmark(bad)

    def test_memory_handoff_validates_ids_and_never_claims_a_write(self):
        selection, _, _ = self.selection()
        note = self.history.memory_note(selection["benchmark_id"], selection["baseline_run_id"])
        self.assertFalse(note["saved_to_memory"])
        arguments = note["memory_tool_arguments"]
        self.assertEqual(("add", "memory"), (arguments["action"], arguments["target"]))
        self.assertIn(selection["benchmark_id"], arguments["content"])
        self.assertNotIn("output_checks", arguments["content"])
        self.assertFalse(list(self.root.rglob("MEMORY.md")))
        ctx = Mock()
        register(ctx)
        self.assertIn("native memory tool", ctx.register_system_prompt_section.call_args.args[1])
        ctx.dispatch_tool.assert_not_called()

    def test_regression_request_requires_execution_matching_runtime_and_explicit_ids(self):
        selection, _, _ = self.selection()
        request = {
            "source": str(self.source),
            "runtime": "custom",
            "execute": True,
            "mode": "regression",
            **{k: selection[k] for k in ("benchmark_id", "baseline_run_id")},
        }
        config = self.service.validate(request)
        self.assertEqual(0, config["limits"]["requests"])
        for changes in (
            {"execute": False},
            {"prior_campaign": "other"},
            {"baseline_run_id": ""},
            {"mode": "explore"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.service.validate({**request, **changes})
        with self.assertRaisesRegex(ValueError, "different|match"):
            self.service.validate({**request, "runtime": "hermes"})

    def test_fixed_runner_rejects_foreign_source_and_honors_queued_stop(self):
        selection, _, config = self.selection()
        config.update(
            mode="regression",
            regression={k: selection[k] for k in ("benchmark_id", "baseline_run_id")},
        )
        bad = copy.deepcopy(config)
        bad["source"]["repository"] = "https://example.com/unrelated"
        factory = Mock(side_effect=AssertionError("Target must not execute"))
        with self.assertRaisesRegex(ValueError, "must match"):
            run_fixed(
                bad,
                self.history,
                on_created=lambda p: None,
                credential_values={},
                adapter_factory=factory,
            )
        output = run_fixed(
            config,
            self.history,
            on_created=lambda p: write_json_atomic(p / "stop.json", {"reason": "test stop"}),
            credential_values={},
            adapter_factory=factory,
        )
        self.assertEqual("operator_preempted", read_json(output / "campaign.json")["stop_reason"])
        factory.assert_not_called()

    def test_fixed_benchmark_cannot_exceed_an_explicit_test_budget(self):
        cid, folder, _ = self.campaign()
        lane = folder / "round_001/trusted"
        extra = case()
        extra["id"] = "task_b"
        write_json_atomic(lane / "evaluator/scenarios/task_b.json", extra)
        write_json_atomic(lane / "execution_input_hashes.json", revision_hashes(lane / "evaluator"))
        write_json_atomic(
            lane / "execution/report.json",
            {"status": "complete", **report({"task_a": [True], "task_b": [True]})},
        )
        selected = self.history.promote(cid)
        request = {
            "source": str(self.source),
            "runtime": "custom",
            "execute": True,
            "mode": "regression",
            **{k: selected[k] for k in ("benchmark_id", "baseline_run_id")},
        }
        self.assertEqual(2, self.service.validate(request)["limits"]["active_tests"])
        with self.assertRaisesRegex(ValueError, "exceeds.*budget"):
            self.service.validate({**request, "limits": {"initial_tests": 1, "active_tests": 1}})

    def test_symlinked_scenarios_are_rejected(self):
        _, folder, _ = self.campaign()
        evaluator = folder / "round_001/trusted/evaluator"
        shutil.rmtree(evaluator / "scenarios")
        (evaluator / "scenarios").symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Linked"):
            tree_hashes(evaluator)
