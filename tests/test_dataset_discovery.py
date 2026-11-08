"""Network-isolated discovery, provenance and workload-grounding regressions."""

import json

import httpx

from src.orchestrator.datasets import (
    CURATED,
    Catalogue,
    discover,
    recovered_manifest,
    user_examples,
    validate_selection,
)
from src.orchestrator.quality import validate_dataset_grounding
from tests.helpers import TemporaryDirectoryTestCase

REVISION = "a" * 40


def catalogue_response(request, requests):
    requests.append(request)
    path = request.url.path
    if path == "/api/datasets":
        return httpx.Response(
            200,
            json=[{"id": "author/coding-sessions", "description": "User coding conversations"}],
        )
    if path == "/api/records":
        return httpx.Response(
            200,
            json={
                "hits": {
                    "hits": [
                        {
                            "id": 123,
                            "metadata": {
                                "title": "Coding sessions",
                                "description": "Public logs",
                                "license": {"id": "cc-by-4.0"},
                            },
                        }
                    ]
                }
            },
        )
    if path.startswith("/api/datasets/"):
        return httpx.Response(
            200,
            json={
                "sha": REVISION,
                "cardData": {"license": "mit"},
                "siblings": [{"rfilename": "session.jsonl"}],
            },
        )
    if path.endswith("README.md"):
        return httpx.Response(200, text="# Public coding sessions")
    if path == "/splits":
        return httpx.Response(200, json={"splits": [{"config": "default", "split": "train"}]})
    if path == "/rows":
        return httpx.Response(
            200,
            json={
                "rows": [
                    {
                        "row_idx": 0,
                        "row": {
                            "messages": [
                                {
                                    "role": "user",
                                    "content": "Fix the failing unit tests in this repository.",
                                },
                                {
                                    "role": "assistant",
                                    "content": "PRIVATE THINKING must not be copied",
                                },
                            ]
                        },
                    }
                ]
            },
        )
    if path.endswith("session.jsonl"):
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Investigate the training job and summarize the errors.",
                    },
                }
            )
            + "\n"
            + '{"partial":',
        )
    return httpx.Response(404)


class DiscoveryTests(TemporaryDirectoryTestCase):
    def test_recovery_uses_hash_verified_original_manifest(self):
        from src.evaluation.runtime_support import sha256_file, write_json_atomic

        self.assertIsNone(recovered_manifest(self.root))
        self.collect("hermes")
        path = self.root / "datasets/manifest.json"
        write_json_atomic(
            self.root / "evidence_index.json",
            {"e1": {"path": str(path), "sha256": sha256_file(path)}},
        )
        material = recovered_manifest(self.root)
        self.assertEqual(CURATED["hermes"], [s["id"] for s in material["sources"]])
        self.assertEqual(str(path), material["reused_from"])
        path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "changed"):
            recovered_manifest(self.root)

    def transport(self, request):
        return catalogue_response(request, self.requests)

    def setUp(self):
        super().setUp()
        self.requests = []

    def collect(self, kind="custom", handler=None, **options):
        config = {"adapter": {"kind": kind}, "scope": "coding research"}
        config.update(options.pop("config", {}))
        with httpx.Client(transport=httpx.MockTransport(handler or self.transport)) as client:
            return discover(config, self.root / "datasets", client=client, **options)

    def test_custom_searches_both_catalogues_and_passes_only_selected_evidence(self):
        observed = []

        def choose(candidates):
            observed.extend(candidates)
            return {
                "selections": [
                    {
                        "id": "author/coding-sessions",
                        "reason": "Repository maintenance matches the source tools",
                    }
                ]
            }

        result = self.collect(
            profile={"purpose": "Coding assistant", "queries": ["coding"]},
            choose=choose,
            environment={"HF_TOKEN": "hf-test-secret"},
        )
        self.assertEqual("ready", result["status"])
        self.assertEqual({"huggingface", "zenodo"}, {c["provider"] for c in observed})
        self.assertEqual(["author/coding-sessions"], [s["id"] for s in result["sources"]])
        source = result["sources"][0]
        self.assertEqual(REVISION, source["revision"])
        self.assertIn(REVISION, source["card_url"])
        self.assertEqual("train", source["samples"][0]["split"])
        self.assertIn("does not pin", source["samples"][0]["revision_note"])
        self.assertNotIn("PRIVATE THINKING", json.dumps(result))
        self.assertTrue(
            any(r.url.params.get("filter") == "format:agent-traces" for r in self.requests)
        )
        for request in self.requests:
            if request.url.host == "zenodo.org":
                self.assertNotIn("authorization", request.headers)
            else:
                self.assertEqual("Bearer hf-test-secret", request.headers["authorization"])
        log = (self.root / "datasets/requests.json").read_text()
        self.assertNotIn("hf-test-secret", log)
        self.assertIn("sha256", log)

    def test_curated_workloads_do_not_send_openhands_to_assistantbench(self):
        for kind in CURATED:
            with self.subTest(kind=kind):
                self.requests.clear()
                result = self.collect(kind)
                self.assertEqual(CURATED[kind], [s["id"] for s in result["sources"]])
                self.assertFalse(any(r.url.path == "/api/datasets" for r in self.requests))
        self.assertIn("McGill-NLP/WebLINX", CURATED["hermes"])
        self.assertNotIn("AssistantBench/AssistantBench", CURATED["openhands"])
        self.assertIn("clem/ml-intern-sessions", CURATED["deerflow"])

    def test_broken_viewer_uses_revision_pinned_jsonl_without_partial_record(self):
        def handler(request):
            if request.url.path == "/rows":
                return httpx.Response(500)
            return self.transport(request)

        result = self.collect("openhands", handler)
        sample = result["sources"][0]["samples"][0]
        self.assertEqual("session.jsonl", sample["file"])
        self.assertEqual(REVISION, sample["revision"])
        self.assertIn("training job", sample["text"])

    def test_gated_sources_remain_card_only_and_all_failed_inspections_stop(self):
        def handler(request):
            if request.url.path in {"/rows", "/splits"} or request.url.path.endswith(".jsonl"):
                return httpx.Response(401)
            return self.transport(request)

        result = self.collect("openhands", handler)
        self.assertEqual("degraded", result["status"])
        self.assertTrue(all(s["sample_status"] == "card_only" for s in result["sources"]))
        self.assertEqual(CURATED["openhands"], [s["id"] for s in result["sources"]])
        with self.assertRaisesRegex(ValueError, "No selected dataset"):
            self.collect("openhands", lambda _: httpx.Response(503))
        saved = json.loads((self.root / "datasets/manifest.json").read_text())
        self.assertEqual("error", saved["status"])
        self.assertTrue(saved["errors"])

    def test_no_search_hits_stops_before_selection(self):
        def handler(request):
            return httpx.Response(
                200, json=[] if request.url.host == "huggingface.co" else {"hits": {"hits": []}}
            )

        with self.assertRaisesRegex(ValueError, "no candidates"):
            self.collect(
                handler=handler,
                profile={"purpose": "Unknown agent", "queries": ["specialized"]},
                choose=lambda _: self.fail("No fabricated selection"),
            )

    def test_explicit_sources_override_defaults_and_disabled_mode_is_offline(self):
        result = self.collect(
            "hermes", config={"dataset_discovery": {"sources": ["author/coding-sessions"]}}
        )
        self.assertEqual("configured", result["selection_method"])
        self.assertEqual(["author/coding-sessions"], [s["id"] for s in result["sources"]])
        result = self.collect(
            handler=lambda _: self.fail("Disabled discovery contacted network"),
            config={"dataset_discovery": {"enabled": False}},
        )
        self.assertEqual("disabled", result["status"])

    def test_unknown_selection_and_false_sample_citations_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "only IDs"):
            validate_selection(
                {"selections": [{"id": "invented/source", "reason": "claimed"}]},
                [{"id": "actual/source"}],
            )
        manifest = {"sources": [{"id": "actual/source", "samples": []}]}
        grounding = {
            "dataset_id": "actual/source",
            "basis": "sampled_user",
            "sample_index": 0,
            "adaptation": "Synthetic fixture",
        }
        plan = {"rationale": [{"workload_source": grounding}]}
        with self.assertRaisesRegex(ValueError, "sample_index"):
            validate_dataset_grounding(plan, manifest)
        grounding.update(basis="metadata")
        validate_dataset_grounding(plan, manifest)
        plan["rationale"][0]["workload_source"] = {
            "architecture_reason": "Checkpoint behavior comes directly from inspected source"
        }
        validate_dataset_grounding(plan, manifest)

    def test_weblinx_and_nested_sessions_exclude_answers_and_scrub_user_excerpts(self):
        instruction = (
            "Book a flight with user@example.com; password: fake-secret\n/home/alice/project"
        )
        history = (
            'say(speaker="instructor", utterance='
            + json.dumps(instruction)
            + ') say(speaker="navigator", utterance="This answer must not be used.")'
        )
        result = user_examples({"action_history": history, "action": "unsafe expected action"})
        self.assertEqual(1, len(result))
        self.assertIn("Book a flight", result[0])
        self.assertNotIn("fake-secret", result[0])
        self.assertNotIn("user@example.com", result[0])
        self.assertNotIn("alice", result[0])
        messages = [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Explain the failing training run."},
                        {"type": "tool_result", "content": "Do not copy tool output"},
                    ],
                },
            },
            {"role": "assistant", "content": "Do not copy answers"},
        ]
        self.assertEqual(["Explain the failing training run."], user_examples(messages))

    def test_response_size_limit_and_stop_boundary(self):
        with httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 2_000_001))
        ) as client:
            catalogue = Catalogue(client, self.root, lambda x: x)
            with self.assertRaisesRegex(ValueError, "exceeds"):
                catalogue.get("https://huggingface.co/api/datasets")
            self.assertEqual(262144, len(catalogue.get("https://huggingface.co/card", prefix=True)))

        def stop():
            raise RuntimeError("operator stopped")

        with self.assertRaisesRegex(RuntimeError, "operator stopped"):
            self.collect(
                "hermes", handler=lambda _: self.fail("Stop must precede request"), check_stop=stop
            )
