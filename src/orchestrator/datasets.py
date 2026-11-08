"""Discover workload sources and retain bounded, attributable dataset evidence.

Only public catalogue APIs and small text samples are read. Dataset code is never
loaded or executed. Downloads and provider credentials are separate from task tools.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from src.evaluation.runtime_support import sha256_file, write_json_atomic
from src.orchestrator.prompts import DATASET_GUIDANCE

CURATED = {
    "hermes": ["AssistantBench/AssistantBench", "McGill-NLP/WebLINX"],
    "openhands": ["SALT-NLP/SWE-chat", "badlogicgames/pi-mono"],
    "deerflow": [
        "clem/ml-intern-sessions",
        "lewtun/ml-intern-sessions",
        "McGill-NLP/WebLINX",
        "AssistantBench/AssistantBench",
    ],
}
WORKLOADS = {
    "hermes": "Personal-assistant, browser dialogue and multi-source research workflows.",
    "openhands": "Real coding conversations, repository changes, debugging and tool use.",
    "deerflow": "Research, ML engineering, analysis and multi-step information synthesis.",
}
DATASET_ID = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def validate_profile(value):
    if not isinstance(value.get("purpose"), str) or not value["purpose"].strip():
        raise ValueError("Dataset discovery needs the agent's purpose")
    queries = value.get("queries")
    if (
        not isinstance(queries, list)
        or not 1 <= len(queries) <= 3
        or any(not isinstance(q, str) or not 2 <= len(q.strip()) <= 100 for q in queries)
    ):
        raise ValueError("Provide one to three short workload dataset search queries")


def validate_selection(value, candidates):
    selected = value.get("selections")
    available = {item["id"] for item in candidates}
    if not isinstance(selected, list) or not 1 <= len(selected) <= 4:
        raise ValueError("Select one to four discovered datasets")
    ids = []
    for item in selected:
        if not isinstance(item, dict) or item.get("id") not in available:
            raise ValueError("Select only IDs returned by dataset discovery")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError("Explain the source's relevance to this agent")
        ids.append(item["id"])
    if len(ids) != len(set(ids)):
        raise ValueError("Dataset selections must be unique")


def sanitize_text(text):
    """Remove common credentials and identifying fields from public excerpts."""
    text = text.replace("\\n", "\n").replace("\\t", " ")
    text = re.sub(
        r"(?im)\b(password|passwd|api[_ -]?key|access[_ -]?token|secret)\s*[:=]\s*[^\s,;]+",
        r"\1: [REDACTED]",
        text,
    )
    text = re.sub(r"\b(?:hf_|sk-|ghp_)[A-Za-z0-9_-]{12,}\b", "[REDACTED_TOKEN]", text)
    text = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[EMAIL]", text)
    text = re.sub(r"(?:/Users/|/home/)[^/\s]+", "/home/[USER]", text)
    return text


def user_examples(value, limit=4):
    """Extract user text only, excluding recorded reasoning and tool results."""
    found = []

    def add(text):
        if isinstance(text, list):
            text = "\n".join(
                part.get("text", "")
                for part in text
                if isinstance(part, dict) and part.get("type") == "text"
            )
        if isinstance(text, str) and len(text.strip()) >= 16 and len(found) < limit:
            text = sanitize_text(text.strip())[:1600]
            if text not in found:
                found.append(text)

    def visit(item, depth=0):
        if depth > 12 or len(found) >= limit:
            return
        if isinstance(item, list):
            for child in item[:100]:
                visit(child, depth + 1)
        elif isinstance(item, dict):
            role = item.get("role", item.get("type"))
            if role in {"assistant", "tool", "thinking", "tool_result"}:
                return
            if role in {"user", "human"}:
                add(item.get("content", item.get("text")))
            for key in ("prompt", "question", "task", "instruction", "utterance"):
                add(item.get(key))
            # WebLINX chat rows encode human instructions in action_history.
            # Parse quoted data; never evaluate the action language or include
            # navigator answers, browser HTML, or expected actions as user text.
            history = item.get("action_history", "")
            if isinstance(history, str):
                for match in re.finditer(
                    r'say\(speaker="instructor",\s*utterance=("(?:[^"\\]|\\.)*")\)', history
                ):
                    try:
                        add(json.loads(match.group(1)))
                    except ValueError:
                        continue
            for key in ("messages", "conversation", "conversations", "message", "turns", "row"):
                child = item.get(key)
                if isinstance(child, str) and child.lstrip().startswith(("[", "{")):
                    try:
                        child = json.loads(child)
                    except ValueError:
                        pass
                visit(child, depth + 1)

    visit(value)
    return found


class Catalogue:
    def __init__(self, client, output, redact, key="", check_stop=lambda: None):
        self.client, self.output, self.redact = client, output, redact
        self.key, self.check_stop = key, check_stop
        self.requests = []

    def get(self, url, params=None, *, prefix=False):
        self.check_stop()
        headers = {}
        if self.key and httpx.URL(url).host in {"huggingface.co", "datasets-server.huggingface.co"}:
            headers["Authorization"] = "Bearer " + self.key
        record = {"url": url, "params": params, "fetched_at": datetime.now(UTC).isoformat()}
        try:
            with self.client.stream("GET", url, params=params, headers=headers) as response:
                record["status"] = response.status_code
                response.raise_for_status()
                content = bytearray()
                bound = 262144 if prefix else 2_000_000
                for chunk in response.iter_bytes():
                    content.extend(chunk[: bound - len(content)])
                    if len(content) >= bound:
                        record["truncated"] = True
                        if not prefix:
                            raise ValueError("Dataset API response exceeds 2 MB")
                        break
            text = content.decode("utf-8", errors="replace")
            record["sha256"] = hashlib.sha256(content).hexdigest()
            return text if prefix else json.loads(text)
        except (httpx.HTTPError, ValueError) as exc:
            record["error"] = self.redact(str(exc))
            raise
        finally:
            self.requests.append(record)
            write_json_atomic(self.output / "requests.json", self.redact(self.requests))

    def search(self, queries):
        candidates, errors = {}, []
        for query in queries:
            for provider in ("huggingface", "huggingface_traces", "zenodo"):
                try:
                    if provider.startswith("huggingface"):
                        parameters = {"search": query, "limit": 5, "full": "true"}
                        if provider == "huggingface_traces":
                            parameters["filter"] = "format:agent-traces"
                        rows = self.get(
                            "https://huggingface.co/api/datasets",
                            parameters,
                        )
                        for row in rows:
                            identity = row.get("id", "")
                            if DATASET_ID.fullmatch(identity) and not row.get("private"):
                                candidates[identity] = {
                                    "id": identity,
                                    "provider": "huggingface",
                                    "url": "https://huggingface.co/datasets/" + identity,
                                    "description": sanitize_text(str(row.get("description", "")))[
                                        :1800
                                    ],
                                    "tags": row.get(
                                        "tags", (row.get("cardData") or {}).get("tags", [])
                                    ),
                                    "gated": row.get("gated", False),
                                }
                    else:
                        # Quote the free-text query so it cannot inject catalogue operators.
                        term = query.replace('"', " ").replace("\\", " ")
                        result = self.get(
                            "https://zenodo.org/api/records",
                            {
                                "q": f'resource_type.type:dataset AND "{term}"',
                                "size": 5,
                            },
                        )
                        for row in result.get("hits", {}).get("hits", []):
                            identity = str(row["id"])
                            if not identity.isdigit():
                                continue
                            meta = row["metadata"]
                            candidates["zenodo:" + identity] = {
                                "id": "zenodo:" + identity,
                                "provider": provider,
                                "url": "https://zenodo.org/records/" + identity,
                                "title": meta.get("title"),
                                "description": sanitize_text(str(meta.get("description", "")))[
                                    :2400
                                ],
                                "license": meta.get("license"),
                                "doi": row.get("doi"),
                            }
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    errors.append({"provider": provider, "query": query, "error": str(exc)})
        return list(candidates.values()), errors

    def inspect(self, candidate):
        result = {**candidate, "samples": [], "sample_status": "unavailable", "errors": []}
        if candidate.get("provider") == "zenodo":
            # This is attributable metadata, not a claim to have inspected an archive.
            result["sample_status"] = "metadata_only"
            return result
        identity = candidate["id"]
        if not DATASET_ID.fullmatch(identity):
            raise ValueError("Invalid Hugging Face dataset ID")
        metadata = self.get("https://huggingface.co/api/datasets/" + identity)
        revision = metadata.get("sha")
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40,64}", revision):
            raise ValueError("Dataset metadata lacks a revision")
        result.update(
            revision=revision,
            description=sanitize_text(metadata.get("description", "") or "")[:2400],
            license=(metadata.get("cardData") or {}).get("license"),
            gated=metadata.get("gated", False),
        )
        base = f"https://huggingface.co/datasets/{identity}/resolve/{revision}/"
        try:
            card = self.get(base + "README.md", prefix=True)
            result["card_excerpt"] = sanitize_text(card)[:10000]
            result["card_url"] = base + "README.md"
        except (httpx.HTTPError, ValueError) as exc:
            result["errors"].append(str(exc))
        try:
            splits = self.get(
                "https://datasets-server.huggingface.co/splits", {"dataset": identity}
            )
            options = splits.get("splits", [])
            options.sort(
                key=lambda s: (
                    s["split"] not in {"train", "validation", "dev"},
                    s["config"] != "conversations",
                )
            )
            for split in options[:2]:
                params = {
                    "dataset": identity,
                    "config": split["config"],
                    "split": split["split"],
                    "offset": 0,
                    "length": 5,
                }
                rows = self.get("https://datasets-server.huggingface.co/rows", params)
                for row in rows.get("rows", []):
                    for text in user_examples(row.get("row", {})):
                        result["samples"].append(
                            {
                                "text": text,
                                "row_idx": row.get("row_idx"),
                                "config": split["config"],
                                "split": split["split"],
                                "url": str(
                                    httpx.URL(
                                        "https://datasets-server.huggingface.co/rows", params=params
                                    )
                                ),
                                "revision_note": "Viewer rows are fetched at the recorded time; the API does not pin rows to the metadata commit.",
                            }
                        )
                if result["samples"]:
                    break
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            result["errors"].append(str(exc))
        if not result["samples"]:
            # Some agent-trace viewers cannot convert session JSONL. Read small,
            # revision-pinned prefixes instead of downloading a complete corpus.
            files = [
                s["rfilename"]
                for s in metadata.get("siblings", [])
                if s["rfilename"].endswith((".jsonl", ".json"))
            ]
            for name in files[:2]:
                url = base + quote(name, safe="/")
                try:
                    raw = self.get(url, prefix=True)
                    try:
                        records = [json.loads(raw)]
                    except ValueError:
                        records = []
                        for line in raw.splitlines():
                            try:
                                records.append(json.loads(line))
                            except ValueError:
                                continue
                    for text in user_examples(records):
                        result["samples"].append(
                            {"text": text, "url": url, "file": name, "revision": revision}
                        )
                except (httpx.HTTPError, ValueError) as exc:
                    result["errors"].append(str(exc))
                if result["samples"]:
                    break
        unique = {}
        for sample in result["samples"]:
            unique.setdefault(sample["text"], sample)
        result["samples"] = list(unique.values())[:4]
        result["sample_status"] = (
            "user_examples"
            if result["samples"]
            else "card_only"
            if result.get("card_excerpt")
            else "metadata_only"
        )
        return result


def recovered_manifest(attempt):
    """Reuse the exact workload material from an interrupted generation/review."""
    path = attempt / "datasets/manifest.json"
    if not path.exists():
        return None
    index = json.loads((attempt / "evidence_index.json").read_text(encoding="utf-8"))
    if not any(
        item["path"] == str(path.resolve()) and item["sha256"] == sha256_file(path)
        for item in index.values()
    ):
        raise ValueError(
            "Recovered dataset manifest is missing its original evidence hash or changed"
        )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("status") not in {"ready", "degraded", "disabled"}:
        raise ValueError("Cannot recover an incomplete dataset-discovery stage")
    return {**manifest, "reused_from": str(path)}


def discover(
    config,
    directory,
    *,
    profile=None,
    choose=None,
    environment=None,
    redact=lambda x: x,
    client=None,
    check_stop=lambda: None,
):
    """Called once before the first generation; all failures remain in the manifest."""
    options = config.get("dataset_discovery", {})
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "sources": [],
        "errors": [],
        "guidance": DATASET_GUIDANCE,
    }
    if options.get("enabled") is False:
        manifest.update(status="disabled", reason="Explicit campaign configuration")
        write_json_atomic(directory / "manifest.json", manifest)
        return manifest
    kind = config.get("adapter", {}).get("kind", "hermes")
    ids = options.get("sources", CURATED.get(kind, []))
    if (
        not isinstance(ids, list)
        or len(ids) > 6
        or any(not isinstance(i, str) or not DATASET_ID.fullmatch(i) for i in ids)
    ):
        raise ValueError(
            "dataset_discovery.sources must contain at most six Hugging Face dataset IDs"
        )
    key = (environment or {}).get(options.get("api_key_env", "HF_TOKEN"), "")
    own_client = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=True)
    catalogue = Catalogue(client, directory, redact, key, check_stop)
    try:
        if ids:
            manifest["selection_method"] = (
                "configured" if "sources" in options else "runtime_defaults"
            )
            manifest["purpose"] = WORKLOADS.get(kind, config["scope"])
            selected = [
                {
                    "id": i,
                    "provider": "huggingface",
                    "url": "https://huggingface.co/datasets/" + i,
                    "reason": manifest["purpose"],
                }
                for i in ids
            ]
        else:
            if profile is None or choose is None:
                raise ValueError(
                    "Custom-agent discovery requires source profiling and source selection"
                )
            validate_profile(profile)
            manifest.update(profile=profile, selection_method="source_guided_search")
            candidates, errors = catalogue.search(profile["queries"])
            manifest["errors"].extend(errors)
            write_json_atomic(directory / "candidates.json", redact(candidates))
            if not candidates:
                raise ValueError(
                    "Dataset searches returned no candidates; configure dataset_discovery.sources or refine the workload queries"
                )
            selection = choose(candidates)
            validate_selection(selection, candidates)
            lookup = {c["id"]: c for c in candidates}
            selected = [{**lookup[s["id"]], "reason": s["reason"]} for s in selection["selections"]]
        for candidate in selected:
            check_stop()
            try:
                inspected = catalogue.inspect(candidate)
                manifest["sources"].append(inspected)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                manifest["errors"].append({"id": candidate["id"], "error": str(exc)})
        if not manifest["sources"]:
            raise ValueError("No selected dataset could be inspected; see datasets/manifest.json")
        manifest["status"] = (
            "degraded"
            if manifest["errors"]
            or any(s["sample_status"] != "user_examples" for s in manifest["sources"])
            else "ready"
        )
        return redact(manifest)
    except Exception as exc:
        manifest.update(status="error", error=str(exc))
        raise
    finally:
        write_json_atomic(directory / "manifest.json", redact(manifest))
        if own_client:
            client.close()
