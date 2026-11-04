"""Bounded generate/review/execute/analyze loop. Run: python -m src.orchestrator."""

import argparse
import hashlib
import json
import shutil
import subprocess
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import httpx

from src.evaluation.api_trace import SecretRedactor
from src.evaluation.audit import audit_campaign
from src.evaluation.differential import compare as differential_compare
from src.evaluation.differential import markdown as differential_markdown
from src.evaluation.evaluation_metrics import api_metrics
from src.evaluation.runtime_support import read_jsonl, sha256_file, write_json_atomic
from src.evaluation.utils import load_eval_config
from src.orchestrator.adapters import ROOT, make_adapter, revision_hashes
from src.orchestrator.prompts import (
    ANALYSIS_REQUIREMENTS,
    ANALYSIS_TASK,
    DESIGN_REQUIREMENTS,
    GENERATION_TASK,
    POLICY,
    RECOVERY_FEEDBACK,
    REPEATED_READ_ADVISORY,
    REVIEWERS,
    SEED_GUIDANCE,
    SEEDED_GENERATION_TASK,
    SMOKE_SCOPE,
    SOURCE_READ_HINT,
    VARIANTS,
    VERSION_ANALYSIS_GUIDANCE,
    VERSION_GENERATION_GUIDANCE,
    contract_error,
    parse_error,
    review_task,
    variant_context,
)
from src.orchestrator.quality import (
    validate_analysis,
    validate_assessments,
    validate_design,
    validate_proposal,
    validate_review,
    validate_stopping,
    validate_version_plan,
)
from src.orchestrator.regression_index import lookup as index_lookup
from src.orchestrator.regression_index import record as index_record
from src.orchestrator.transcripts import export_execution
from src.orchestrator.workspace import Workspace


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class CampaignPreempted(Exception):
    """Operator-requested stop at a safe boundary, never a convergence verdict."""


def check_stop(directory):
    path = Path(directory) / "stop.json"
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        reason = value.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("stop.json requires a nonempty reason")
        raise CampaignPreempted(reason)


class Evidence:
    def __init__(self, redactor):
        self.redactor = redactor
        self.items = {}
        self.seen = set()

    def add(self, path):
        path = Path(path).resolve()
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        eid = "e" + str(len(self.items) + 1)
        self.items[eid] = {"path": str(path), "sha256": sha, "bytes": len(raw)}
        return eid

    def read(self, query):
        identity = query.get("id")
        if isinstance(identity, str) and identity in self.seen:
            eid, start, end = identity.split(":")
            query = {
                **query,
                "id": eid,
                "offset": query.get("offset", int(start)),
                "length": query.get("length", max(1, int(end) - int(start))),
            }
            identity = eid
        if not isinstance(identity, str) or identity not in self.items:
            raise ValueError(
                "Unknown evidence ID. For workspace files use actions with op=read and path. For agent source use source with path."
            )
        item = self.items[query["id"]]
        raw = Path(item["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("Evidence changed since indexing")
        value = self.redactor(raw.decode("utf-8-sig"))
        offset, length = query.get("offset", 0), query.get("length", 12000)
        if type(offset) is not int or type(length) is not int or offset < 0 or length < 1:
            raise ValueError("Invalid evidence range")
        if offset > len(value):
            raise ValueError("Offset beyond evidence")
        text = value[offset : offset + length]
        ref = f"{query['id']}:{offset}:{offset + len(text)}"
        self.seen.add(ref)
        return {
            "ref": ref,
            "sha256": item["sha256"],
            "text": text,
            "total_characters": len(value),
            "more": offset + len(text) < len(value),
        }


class Client:
    def __init__(self, config, directory, redactor, key, limit):
        self.config, self.directory, self.redactor = config, directory, redactor
        self.key, self.limit, self.calls = key, limit, 0

    def ask(self, messages):
        """Retry transient inference failures, never replay local tool actions."""
        retries = self.config.get("transient_retries", 5)
        for attempt in range(retries + 1):
            try:
                return self._ask_once(messages)
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                status = (
                    exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                )
                if status is not None and status not in {408, 429, 500, 502, 503, 504}:
                    raise
                if attempt == retries:
                    raise
                delay = min(60, 5 * 2**attempt)
                if isinstance(exc, httpx.HTTPStatusError):
                    try:
                        delay = max(delay, float(exc.response.headers.get("retry-after", "0")))
                    except ValueError:
                        pass
                save(
                    self.directory / f"api_{self.calls:04d}.retry.json",
                    {
                        "attempt": attempt + 1,
                        "status": status,
                        "error_type": type(exc).__name__,
                        "delay_seconds": delay,
                        "local_actions_replayed": False,
                    },
                )
                print(
                    f"Transient orchestrator API failure ({status or type(exc).__name__}); retrying in {delay}s",
                    flush=True,
                )
                # Short sleeps permit interruption even with a long Retry-After.
                remaining = delay
                while remaining > 0:
                    interval = min(remaining, 30)
                    time.sleep(interval)
                    remaining -= interval

    def _ask_once(self, messages):
        check_stop(self.directory)
        if self.limit is not None and self.calls >= self.limit:
            raise RuntimeError("Orchestrator request budget exhausted")
        self.calls += 1
        body = {
            "model": self.config["model"],
            "messages": messages,
            "stream": False,
        }
        if "max_output_tokens" in self.config:
            body["max_tokens"] = self.config["max_output_tokens"]
        input_limit = self.config.get("max_request_characters")
        if input_limit is not None and len(canonical(body)) > input_limit:
            raise ValueError(
                "Orchestrator input budget exceeded; reduce scope or supplied diagrams"
            )
        if "reasoning_effort" in self.config:
            body["reasoning_effort"] = self.config["reasoning_effort"]
        prefix = self.directory / f"api_{self.calls:04d}"
        save(prefix.with_suffix(".request.json"), self.redactor(body))
        # Each dispatch has distinct evidence; inference retries do not execute actions.
        started = time.monotonic()
        try:
            timeout = httpx.Timeout(
                self.config.get("timeout_seconds", 180),
                read=self.config.get("read_timeout_seconds"),
            )
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    self.config["base_url"].rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.key},
                    json=body,
                )
        except httpx.HTTPError as exc:
            save(
                prefix.with_suffix(".error.json"),
                self.redactor({"error": str(exc), "type": type(exc).__name__}),
            )
            raise
        save(
            prefix.with_suffix(".response.json"),
            self.redactor(
                {
                    "status": response.status_code,
                    "headers": dict(response.headers),
                    "body": response.text,
                    "elapsed_seconds": time.monotonic() - started,
                }
            ),
        )
        response.raise_for_status()
        payload = response.json()
        if payload["choices"][0].get("finish_reason") != "stop":
            raise ValueError("Orchestrator output incomplete or unsupported finish reason")
        content = payload["choices"][0]["message"]["content"].strip()
        if content.startswith("```") and content.endswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        # Some compatible providers prepend conversational text despite JSON instructions.
        # Preserve the full wire body above and accept exactly one complete JSON object.
        start = content.find("{")
        if start < 0:
            raise json.JSONDecodeError("No JSON object in response", content, 0)
        result, end = json.JSONDecoder().raw_decode(content, start)
        if "{" in content[end:]:
            raise json.JSONDecodeError("Multiple JSON objects in response", content, end)
        if not isinstance(result, dict):
            raise ValueError("Orchestrator response must be a JSON object")
        return result


def save(path, value):
    # Campaign files are append-only by name; never replace historical evidence.
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)


def validation_evidence(validation, directory, evidence):
    """Make preflight diagnostics readable before review, not only after execution."""
    directory = Path(directory).resolve()
    files, failures = {}, []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(directory):
            continue
        if path.suffix not in {".json", ".jsonl", ".log", ".txt"}:
            continue
        eid = evidence.add(path)
        files[eid] = {
            **evidence.items[eid],
            "relative_path": path.relative_to(directory).as_posix(),
        }
        if path.suffix != ".json":
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            continue
        if not isinstance(report, dict) or "scenario_contract" not in report:
            continue
        for turn in report.get("reference_prefix", []) + report.get("turns", []):
            checks = {
                group: turn[group + "_check"]
                for group in ("state", "process", "output")
                if turn.get(group + "_check", {}).get("passed") is False
            }
            if checks or turn.get("harness_errors") or turn.get("runtime_errors"):
                failures.append(
                    {
                        "scenario_id": report.get("id"),
                        "turn_number": turn.get("turn_number"),
                        "report_evidence_id": eid,
                        "failed_checks": checks,
                        "harness_errors": turn.get("harness_errors", []),
                        "runtime_errors": turn.get("runtime_errors", []),
                    }
                )
    return {**validation, "reference_failures": failures, "evidence": files}


def exchange(
    client,
    evidence,
    task,
    *,
    workspace=None,
    readonly=False,
    journal=None,
    source=None,
    response_validator=None,
    conversation=None,
    continue_pending=False,
):
    content = [{"type": "text", "text": canonical(task)}]
    messages = conversation if conversation is not None else []
    if not messages:
        messages.append({"role": "system", "content": POLICY})
    if not continue_pending:
        messages.append({"role": "user", "content": content})
    elif len(messages) < 2:
        raise ValueError("Pending-request recovery requires its saved conversation")
    read_counts = {}
    source_paths = None
    while True:
        try:
            answer = client.ask(messages)
        except json.JSONDecodeError as exc:
            messages.append(
                {
                    "role": "user",
                    "content": parse_error(exc),
                }
            )
            continue
        if not {"read", "actions", "source"}.intersection(answer):
            if response_validator:
                try:
                    response_validator(answer)
                except (ValueError, KeyError, TypeError) as exc:
                    messages.extend(
                        [
                            {"role": "assistant", "content": canonical(answer)},
                            {
                                "role": "user",
                                "content": contract_error(exc),
                            },
                        ]
                    )
                    continue
            messages.append({"role": "assistant", "content": canonical(answer)})
            return answer
        results = []
        try:
            if sum(name in answer for name in ("read", "actions", "source")) != 1:
                raise ValueError("Use one operation envelope per response")
            if "source" in answer:
                if source is None or not isinstance(answer["source"], list) or not answer["source"]:
                    raise ValueError("Invalid source request")
                for query in answer["source"]:
                    try:
                        if "query" in query:
                            if "path" in query:
                                raise ValueError(
                                    "Use query for path discovery or path for a source read"
                                )
                            term = query["query"]
                            offset, limit = query.get("offset", 0), query.get("limit", 25)
                            if (
                                not isinstance(term, str)
                                or not term.strip()
                                or type(offset) is not int
                                or offset < 0
                                or type(limit) is not int
                                or limit < 1
                            ):
                                raise ValueError(
                                    "Source search needs nonempty query, offset >= 0 and limit > 0"
                                )
                            if source_paths is None:
                                source_paths = (
                                    subprocess.run(
                                        ["git", "-C", str(source), "ls-files", "-z"],
                                        check=True,
                                        capture_output=True,
                                        text=True,
                                        encoding="utf-8",
                                    )
                                    .stdout.rstrip("\0")
                                    .split("\0")
                                )
                            matches = [p for p in source_paths if term.casefold() in p.casefold()]
                            results.append(
                                {
                                    "query": query,
                                    "paths": matches[offset : offset + limit],
                                    "total": len(matches),
                                    "has_more": offset + limit < len(matches),
                                    "scope": "Tracked path names only; read source content before citing architecture.",
                                }
                            )
                            continue
                        path = (source / query["path"]).resolve()
                        if (
                            not path.is_relative_to(source)
                            or ".git" in path.relative_to(source).parts
                            or path.suffix not in {".py", ".md", ".toml", ".yaml", ".json"}
                        ):
                            raise ValueError("Source read outside allowed repository files")
                        eid = evidence.add(path)
                        results.append({"query": query, **evidence.read({**query, "id": eid})})
                    except (
                        ValueError,
                        KeyError,
                        OSError,
                        TypeError,
                        AttributeError,
                        subprocess.CalledProcessError,
                    ) as exc:
                        results.append(
                            {
                                "query": query,
                                "error": str(exc),
                                "hint": SOURCE_READ_HINT,
                            }
                        )
            elif "read" in answer:
                if not isinstance(answer["read"], list) or not answer["read"]:
                    raise ValueError("Invalid evidence request count")
                for query in answer["read"]:
                    try:
                        results.append({"query": query, **evidence.read(query)})
                    except (ValueError, KeyError, OSError, TypeError, AttributeError) as exc:
                        results.append({"query": query, "error": str(exc)})
            else:
                actions = answer["actions"]
                if workspace is None or not isinstance(actions, list) or not actions:
                    raise ValueError("Invalid workspace action batch")
                for action in actions:
                    try:
                        results.append(
                            {"action": action, "result": workspace.action(action, readonly)}
                        )
                    except (ValueError, KeyError, OSError, TypeError, AttributeError) as exc:
                        results.append({"action": action, "error": str(exc)})
        except (ValueError, KeyError, OSError, TypeError, AttributeError) as exc:
            results = [{"error": str(exc)}]
        for result in results:
            action = result.get("action", {})
            query = result.get("query", {})
            data = result.get("result", {}) if action else result
            if result.get("error") or (action and action.get("op") != "read"):
                continue
            if not isinstance(data, dict) or "text" not in data or "sha256" not in data:
                continue
            # Compare the returned range, not the requested length: a short file
            # read with length=4000 and length=16000 is still the same observation.
            key = canonical(
                [
                    "workspace" if action else "source" if "source" in answer else "evidence",
                    action.get("path") or query.get("path") or query.get("id"),
                    action.get("offset", 0) if action else query.get("offset", 0),
                    data["sha256"],
                    data["text"],
                ]
            )
            read_counts[key] = read_counts.get(key, 0) + 1
            if read_counts[key] >= 3:
                result["repeated_read"] = {
                    "count": read_counts[key],
                    "advisory": REPEATED_READ_ADVISORY,
                }
        if journal:
            with Path(journal).open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        evidence.redactor({"request": answer, "results": results}),
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        messages.extend(
            [
                {"role": "assistant", "content": canonical(answer)},
                {"role": "user", "content": canonical(results)},
            ]
        )


def prepare_source(config, output):
    source_config = config["source"]
    repository = source_config["repository"]
    revision = source_config["revision"]
    source = _checkout(
        repository, revision, source_config.get("local_path", output / "agent_source")
    )
    paths = (
        subprocess.check_output(["git", "-C", str(source), "ls-files", "-z"], text=True)
        .rstrip("\0")
        .split("\0")
    )
    save(
        output / "agent_source.json",
        {"repository": repository, "revision": revision, "files": paths},
    )
    return source


def _checkout(repository, revision, path):
    """Clone (if needed) and verify a clean detached checkout of one revision."""
    path = Path(path).resolve()
    if not path.exists():
        subprocess.run(["git", "clone", "--depth", "1", repository, str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "fetch", "origin", revision], check=True)
        subprocess.run(["git", "-C", str(path), "checkout", "--detach", revision], check=True)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()

    if git("rev-parse", "HEAD") != revision:
        raise ValueError(f"Agent revision mismatch at {path}: expected {revision}")
    # A local Git checkout can have no origin. Its file URI identifies that source.
    if repository != path.as_uri() and git("remote", "get-url", "origin").removesuffix(
        ".git"
    ) != repository.removesuffix(".git"):
        raise ValueError("Agent repository remote does not match configuration")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError(f"Agent source at {path} contains tracked modifications")
    return path


def prepare_version_change(config, output, source):
    """Check out the base revision and record what changed between base and target.

    Writes version_change.json (changed files, diff stat, commit log) and
    version_change.diff (the full diff) for the main agent to read as evidence.
    """
    source_config = config["source"]
    repository = source_config["repository"]
    base, target = source_config["base_revision"], source_config["revision"]
    if base == target:
        raise ValueError("base_revision must differ from revision")
    base_source = _checkout(
        repository, base, source_config.get("base_local_path", output / "agent_source_base")
    )

    def git(*args, check=True):
        result = subprocess.run(
            ["git", "-C", str(source), *args], capture_output=True, text=True, check=False
        )
        if check and result.returncode:
            raise ValueError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    if git("cat-file", "-e", f"{base}^{{commit}}", check=False).returncode:
        git("fetch", "origin", base)  # make the base tree available for diffing
    changed = git("diff", "--name-only", "-z", base, target).stdout.rstrip("\0").split("\0")
    changed = [path for path in changed if path]
    if not changed:
        raise ValueError("No file changes between base_revision and revision")
    diff = git("diff", base, target).stdout
    log = git("log", "--oneline", f"{base}..{target}", check=False)
    summary = {
        "repository": repository,
        "base_revision": base,
        "target_revision": target,
        "changed_files": changed,
        "diff_stat": git("diff", "--stat", base, target).stdout,
        "commit_log": log.stdout if log.returncode == 0 else None,
        "commit_log_note": None
        if log.returncode == 0
        else "Commit history unavailable (shallow clone); use base_local_path/local_path with full history.",
        "diff_characters": len(diff),
    }
    save(output / "version_change.json", summary)
    (output / "version_change.diff").write_text(diff, encoding="utf-8")
    return base_source, summary


def _load_report(path):
    path = Path(path)
    if not path.exists():
        return None
    report = json.loads(path.read_text(encoding="utf-8"))
    return report if "cases" in report else None


def _index_lane(index_path, report_path, revision, **details):
    """Register an original-evaluator lane in the results index (complete reports only)."""
    if _load_report(report_path) is None:
        return None
    return index_record(index_path, revision, report_path, kind="orchestrator_lane", **details)


def _version_differential(directory, plan, record, change, evidence):
    """Compare base_version (old framework) with trusted (new framework) for one round."""
    base = _load_report(directory / "base_version/execution/report.json")
    target = _load_report(directory / "trusted/execution/report.json")
    if base is None or target is None:
        missing = [
            lane for lane, report in (("base_version", base), ("trusted", target)) if report is None
        ]
        return {"available": False, "missing_complete_reports": missing}
    rows = {row.get("id"): row for row in plan.get("rationale", []) if isinstance(row, dict)}
    layers = {sid: row.get("property_layer", "unlabelled") for sid, row in rows.items()}
    result = differential_compare(base, target, layers)
    for row in result["table"]:
        rationale = rows.get(row["id"], {})
        row["change_target"] = rationale.get("change_target")
        row["predicted_outcome"] = rationale.get("predicted_outcome")
    result.update(
        available=True,
        base_revision=change["base_revision"],
        target_revision=change["target_revision"],
        comparison="base_version lane vs trusted lane: same scenarios and original evaluator",
    )
    save(directory / "version_differential.json", result)
    (directory / "version_differential.md").write_text(
        differential_markdown(
            result,
            baseline_label=f"base {change['base_revision'][:10]}",
            candidate_label=f"target {change['target_revision'][:10]}",
        ),
        encoding="utf-8",
    )
    result["evidence_id"] = evidence.add(directory / "version_differential.json")
    record["version_differential"] = str(directory / "version_differential.json")
    return result


def api_usage(directory):
    totals = {"input_tokens": 0, "output_tokens": 0}
    by_role = {}
    missing = []
    requests = sorted(Path(directory).glob("api_*.request.json"))
    for request in requests:
        try:
            body = json.loads(request.read_text(encoding="utf-8"))
            task = json.loads(body["messages"][1]["content"][0]["text"]).get("task", "")
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            task = ""
        role = next(
            (name for name in REVIEWERS if f"as {name}." in task),
            "dashboard_findings"
            if task.startswith("Present dashboard findings")
            else "analysis"
            if task.startswith("Analyze")
            else "main",
        )
        group = by_role.setdefault(
            role, {"requests": 0, "input_tokens": 0, "output_tokens": 0, "elapsed_seconds": 0}
        )
        group["requests"] += 1
        response = request.with_name(request.name.replace(".request.json", ".response.json"))
        try:
            recorded = json.loads(response.read_text(encoding="utf-8"))
            group["elapsed_seconds"] += recorded.get("elapsed_seconds", 0)
            payload = json.loads(recorded["body"])
            usage = payload.get("usage", {})
            incoming = usage.get("prompt_tokens", usage.get("input_tokens"))
            outgoing = usage.get("completion_tokens", usage.get("output_tokens"))
            if type(incoming) is not int or type(outgoing) is not int:
                raise ValueError("Missing usage")
            totals["input_tokens"] += incoming
            totals["output_tokens"] += outgoing
            group["input_tokens"] += incoming
            group["output_tokens"] += outgoing
        except (OSError, ValueError, KeyError):
            missing.append(request.name)
    return {
        "requests": len(requests),
        "reported_tokens": totals,
        "by_role": by_role,
        "usage_missing": missing,
    }


def target_api_usage(directory, prices=None):
    """Count target requests, including preflight and auxiliary inference, once."""
    directory = Path(directory)
    requests, seen_events, errors, sources = {}, set(), [], []
    for path in sorted(directory.rglob("api.jsonl")):
        relative = path.relative_to(directory)
        parts = relative.parts
        stage = (
            "generation_validation"
            if any(part.startswith("generation_") for part in parts)
            else "_".join(parts[1:3])
            if len(parts) > 3
            else "other"
        )
        try:
            events = read_jsonl(path)
            if any(
                not isinstance(event, dict)
                or not isinstance(event.get("event"), str)
                or (
                    event["event"]
                    in {
                        "api_request",
                        "api_response_body",
                        "api_response_headers",
                        "api_response_end",
                        "api_stream_event",
                        "api_request_error",
                    }
                    and not isinstance(event.get("request_id"), str)
                )
                or (
                    event["event"] == "api_stream_event" and not isinstance(event.get("frame"), str)
                )
                or (
                    event["event"] == "api_request"
                    and (
                        not isinstance(event.get("request_id"), str)
                        or not isinstance(event.get("body", {}), dict)
                    )
                )
                for event in events
            ):
                raise ValueError("Malformed API event/request record")
        except (OSError, ValueError) as exc:
            errors.append({"source": str(relative), "error": str(exc)})
            continue
        sources.append(str(relative))
        for event in events:
            if event["event"] != "api_request":
                continue
            rid = event["request_id"]
            if rid in requests:
                if requests[rid]["request"] != event:
                    errors.append(
                        {"source": str(relative), "error": f"Conflicting request ID {rid}"}
                    )
            else:
                requests[rid] = {
                    "request": event,
                    "events": [],
                    "stage": stage,
                    "phase": event.get("phase") or "unknown",
                }
        for event in events:
            rid = event.get("request_id")
            if not rid:
                continue
            if rid not in requests:
                errors.append({"source": str(relative), "error": f"Missing request for {rid}"})
                continue
            identity = hashlib.sha256(canonical(event).encode()).digest()
            if identity not in seen_events:
                seen_events.add(identity)
                requests[rid]["events"].append(event)
    stages, phases = defaultdict(list), defaultdict(list)
    all_events = []
    for value in requests.values():
        stages[value["stage"]].extend(value["events"])
        phases[value["phase"]].extend(value["events"])
        all_events.extend(value["events"])
    total = api_metrics(all_events, prices)
    if errors:
        total["usage_complete"] = False
        total["estimated_cost_usd"] = None
    return {
        "totals": total,
        "by_stage": {stage: api_metrics(events, prices) for stage, events in stages.items()},
        "by_runtime_phase": {
            phase: api_metrics(events, prices) for phase, events in phases.items()
        },
        "sources": sources,
        "errors": errors,
        "scope": "Unique target API requests under this campaign directory, including authored-reference preflight, task execution, diagnostics and auxiliary shutdown/checkpoint inference. Recovered ancestor campaigns and separate external probes are not included. Counts are independent of recorded task pass/fail. Dollar estimates need explicit prices; missing usage is not zero cost.",
    }


def initial_recovery(config, evidence):
    """Recover an unanswered first-generation request without replaying local actions."""
    if not config.get("resume_initial_from"):
        return None
    prior = Path(config["resume_initial_from"]).resolve()
    if not prior.is_relative_to(
        Path(config.get("artifacts_root", ROOT / "eval_harness/orchestrator")).resolve()
    ):
        raise ValueError("Recovery must name a local orchestrator campaign")
    state = json.loads((prior / "campaign.json").read_text(encoding="utf-8"))
    rounds = state.get("rounds", [])
    if (
        state.get("stop_reason") != "error"
        or len(rounds) != 1
        or rounds[0].get("generations")
        or rounds[0].get("executions")
    ):
        raise ValueError("Recovery currently supports failures within the first generation only")
    previous = json.loads((prior / "config.json").read_text(encoding="utf-8-sig"))
    for field in ("scope", "source", "target", "adapter", "limits"):
        if previous[field] != config[field]:
            raise ValueError(f"Recovery cannot change {field}")
    timing = {"timeout_seconds", "read_timeout_seconds"}
    if {k: v for k, v in previous["orchestrator"].items() if k not in timing} != {
        k: v for k, v in config["orchestrator"].items() if k not in timing
    }:
        raise ValueError("Recovery can change only orchestrator network timeouts")
    if revision_hashes(prior / "working") != revision_hashes(prior / "trusted"):
        raise ValueError("Recovery requires an unmodified initial workspace")
    request = sorted(prior.glob("api_*.request.json"))[-1]
    failure = request.with_name(request.name.replace(".request.json", ".error.json"))
    response = request.with_name(request.name.replace(".request.json", ".response.json"))
    if (
        response.exists()
        or json.loads(failure.read_text(encoding="utf-8")).get("type") != "ReadTimeout"
    ):
        raise ValueError("Recovery requires an unanswered read-timeout request")
    body = json.loads(request.read_text(encoding="utf-8"))
    evidence.items = json.loads((prior / "evidence_index.json").read_text(encoding="utf-8"))
    for item in evidence.items.values():
        if sha256_file(Path(item["path"])) != item["sha256"]:
            raise ValueError("Recovery evidence changed")
    messages = body["messages"]
    for message in messages:
        if message.get("role") != "user" or not isinstance(message.get("content"), str):
            continue
        try:
            values = json.loads(message["content"])
        except ValueError:
            continue
        if isinstance(values, list):
            evidence.seen.update(v["ref"] for v in values if isinstance(v, dict) and "ref" in v)
    task = json.loads(messages[1]["content"][0]["text"])
    return {
        "prior": prior,
        "request": request,
        "messages": messages,
        "source_id": task["source_index"],
    }


def review_recovery(config, evidence):
    """Resume a stopped review at the next generation, without repeating target runs.

    The interrupted generation remains evidence, including its incomplete review.
    This is an explicit new revision, not an exact replay of the failed request.
    """
    if not config.get("resume_from"):
        return None
    prior = Path(config["resume_from"]).resolve()
    if not prior.is_relative_to(
        Path(config.get("artifacts_root", ROOT / "eval_harness/orchestrator")).resolve()
    ):
        raise ValueError("Recovery must name a local orchestrator campaign")
    state = json.loads((prior / "campaign.json").read_text(encoding="utf-8"))
    previous = json.loads((prior / "config.json").read_text(encoding="utf-8-sig"))
    for field in ("scope", "source", "target", "adapter", "limits"):
        if previous.get(field) != config.get(field):
            raise ValueError(f"Recovery cannot change {field}")
    for field in ("model", "base_url", "api_key_env"):
        if previous["orchestrator"].get(field) != config["orchestrator"].get(field):
            raise ValueError(f"Recovery cannot change orchestrator {field}")
    prior_stop = state.get("stop_reason")
    operator_reason = state.get("preemption_reason")
    if prior_stop not in {"error", "operator_preempted"} or not state.get("rounds"):
        raise ValueError("Review recovery requires a failed or operator-preempted campaign")
    if prior_stop == "operator_preempted" and (
        not isinstance(operator_reason, str) or not operator_reason.strip()
    ):
        raise ValueError("Operator recovery requires the recorded preemption reason")
    record = state["rounds"][-1]
    if record.get("executions"):
        raise ValueError("Review recovery cannot resume an execution or analysis failure")
    stages = sorted((prior / f"round_{record['round']:03d}").glob("generation_*"))
    stage = stages[-1]
    generation = int(stage.name.split("_")[-1])
    if generation >= config.get("limits", {}).get("generations", 3):
        raise ValueError("No generation remains for review recovery")
    plan = json.loads((stage / "proposal.json").read_text(encoding="utf-8"))
    validation = json.loads((stage / "validation.json").read_text(encoding="utf-8"))
    if revision_hashes(prior / "working") != revision_hashes(stage / "candidate"):
        raise ValueError("Workspace changed after the interrupted review snapshot")
    reviews = {
        role: json.loads(path.read_text(encoding="utf-8"))
        for role in REVIEWERS
        if (path := stage / f"review_{role}.json").exists()
    }
    if len(reviews) == len(REVIEWERS):
        raise ValueError("No interrupted review to recover")
    evidence.items = json.loads((prior / "evidence_index.json").read_text(encoding="utf-8"))
    for item in evidence.items.values():
        if sha256_file(Path(item["path"])) != item["sha256"]:
            raise ValueError("Recovery evidence changed")

    def restore_refs(value):
        if isinstance(value, dict):
            if isinstance(value.get("ref"), str):
                eid, start, end = value["ref"].split(":")
                evidence.read({"id": eid, "offset": int(start), "length": int(end) - int(start)})
            for child in value.values():
                restore_refs(child)
        elif isinstance(value, list):
            for child in value:
                restore_refs(child)

    main_messages = None
    source_id = None
    for request in sorted(prior.glob("api_*.request.json")):
        messages = json.loads(request.read_text(encoding="utf-8"))["messages"]
        for message in messages:
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, list):
                content = content[0].get("text", "") if content else ""
            try:
                task = json.loads(content)
            except (ValueError, TypeError):
                continue
            restore_refs(task)
            if (
                isinstance(task, dict)
                and task.get("round") == record["round"]
                and str(task.get("task", "")).startswith("Generate")
            ):
                main_messages = messages
                source_id = task["source_index"]
    if main_messages is None:
        raise ValueError("Missing saved main-agent conversation")
    main_messages[0] = {"role": "system", "content": POLICY}
    main_messages.append({"role": "assistant", "content": canonical(plan)})
    if not any(g["generation"] == generation for g in record["generations"]):
        record["generations"].append(
            {
                "generation": generation,
                "validation": validation,
                "reviews": reviews,
                "proposal": str(stage / "proposal.json"),
                "interrupted_review": [r for r in REVIEWERS if r not in reviews],
            }
        )
    state.pop("error", None)
    state.pop("preemption_reason", None)
    state["stop_reason"] = "error"
    return {
        "prior": prior,
        "prior_stop_reason": prior_stop,
        "prior_preemption_reason": operator_reason,
        "state": state,
        "round": record["round"],
        "generation": generation + 1,
        "messages": main_messages,
        "source_id": source_id,
        "feedback": [{"role": r, **v} for r, v in reviews.items()]
        + [
            {
                "role": "recovery",
                "description": (
                    "An operator stopped the review to revise the test design: "
                    + operator_reason
                    + " The interrupted generation counts toward the original limit. "
                    "Revise the saved suite using available reviewer findings and this feedback. "
                    "This was not a provider failure; no target executions are replayed."
                    if prior_stop == "operator_preempted"
                    else RECOVERY_FEEDBACK
                ),
            }
        ],
    }


def run(
    config, client_factory=Client, adapter_factory=make_adapter, execute=False, on_created=None
):
    if {"resume_final_execution_from", "recovery_evaluator_repairs"}.intersection(config):
        raise ValueError(
            "One-off historical evaluator repairs are retired. "
            "Use the original frozen implementation to reproduce those campaigns."
        )
    variant = config.get("prompt_variant", "baseline")
    if variant not in VARIANTS:
        raise ValueError(f"Unknown prompt_variant: {variant}")
    artifact_root = Path(config.get("artifacts_root", ROOT / "eval_harness/orchestrator")).resolve()
    attempt = uuid.uuid4().hex
    prior = config.get("resume_from") or config.get("resume_initial_from")
    if prior and Path(prior).parent.name == "attempts":
        experiment = Path(prior).resolve().parents[1]
        if not experiment.is_relative_to(artifact_root):
            raise ValueError("Recovery must name a local orchestrator experiment")
    else:
        kind = config.get("adapter", {}).get("kind", "hermes")
        if kind not in {"hermes", "openhands", "deerflow"}:
            raise ValueError("Unknown agent runtime")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        experiment = artifact_root / f"{kind}_{stamp}_{attempt[:8]}"
    output = experiment / "attempts" / attempt
    output.mkdir(parents=True)
    values = load_eval_config(ROOT / ".env")
    orch_key = values.get(config["orchestrator"].get("api_key_env", ""), "")
    target_key = values.get(config["target"].get("api_key_env", ""), "")
    redactor = SecretRedactor([orch_key, target_key])
    save(output / "config.json", redactor(config))
    evidence = Evidence(redactor)
    state = {
        "rounds": [],
        "stop_reason": "error",
        "scope": config["scope"],
        "prompt_variant": variant,
    }
    save(
        output / "prompt_manifest.json",
        {
            "variant": variant,
            "prompt_source_sha256": sha256_file(Path(__file__).with_name("prompts.py")),
            "guidance": {
                role: variant_context(variant, role) for role in ("main", *REVIEWERS, "analysis")
            },
        },
    )

    def progress(phase, **details):
        check_stop(output)
        write_json_atomic(
            output / "progress.json",
            {"phase": phase, "updated_at": datetime.now(UTC).isoformat(), **details},
        )
        write_json_atomic(output / "campaign.json", redactor(state))

    print(output, flush=True)
    try:
        if on_created:
            on_created(output)
        limits = {
            "rounds": 3,
            "initial_tests": 5,
            "active_tests": 12,
            "generations": 3,
            **config.get("limits", {}),
        }
        for key, maximum in (
            ("rounds", 5),
            ("initial_tests", 5),
            ("active_tests", 12),
            ("generations", 3),
        ):
            if type(limits[key]) is not int or not 1 <= limits[key] <= maximum:
                raise ValueError(f"{key} must be between 1 and {maximum}")
        if limits["initial_tests"] > limits["active_tests"]:
            raise ValueError("Initial tests exceed active test cap")
        progress("source")
        source = prepare_source(config, output)
        version_mode = bool(config["source"].get("base_revision"))
        version_request, base_adapter, change = None, None, None
        index_path = Path(
            config.get("regression_index", ROOT / "eval_harness/regression_index.json")
        )
        if version_mode:
            if config.get("adapter", {}).get("kind", "hermes") != "hermes":
                raise ValueError("Version regression mode currently supports Hermes only")
            base_source, change = prepare_version_change(config, output, source)
            prior_results = index_lookup(index_path, change["base_revision"])
            save(
                output / "prior_results.json",
                {"base_revision": change["base_revision"], "entries": prior_results},
            )
            version_request = {
                "base_revision": change["base_revision"],
                "target_revision": change["target_revision"],
                "changed_files": change["changed_files"],
                "change_summary_evidence_id": evidence.add(output / "version_change.json"),
                "diff_evidence_id": evidence.add(output / "version_change.diff"),
                "prior_results_evidence_id": evidence.add(output / "prior_results.json"),
                "prior_result_count": len(prior_results),
                "guidance": VERSION_GENERATION_GUIDANCE,
            }
            state["version_change"] = {
                k: version_request[k]
                for k in ("base_revision", "target_revision", "changed_files", "prior_result_count")
            }
            base_adapter = adapter_factory(
                {
                    **config["adapter"],
                    "hermes_root": str(base_source),
                    "source_revision": change["base_revision"],
                    "target": config["target"],
                }
            )
        resumed = review_recovery(config, evidence)
        recovery = initial_recovery(config, evidence) if not resumed else None
        source_id = (
            resumed["source_id"]
            if resumed
            else recovery["source_id"]
            if recovery
            else evidence.add(output / "agent_source.json")
        )
        if resumed:
            workspace = Workspace.clone(output / "working", resumed["prior"] / "working")
            trusted = Workspace.clone(output / "trusted", resumed["prior"] / "trusted")
            state = resumed["state"]
            state["recovery"] = {
                "prior_campaign": str(resumed["prior"]),
                "method": "next_generation_after_interrupted_review",
                "round": resumed["round"],
                "generation": resumed["generation"],
                "prior_stop_reason": resumed["prior_stop_reason"],
                "prior_preemption_reason": resumed["prior_preemption_reason"],
                "prompt_policy": "Current controller policy and variant guidance; prior conversation retained as evidence, not an exact request replay.",
            }
            save(output / "recovery.json", state["recovery"])
        else:
            workspace = Workspace(
                output / "working", recovery["prior"] / "working" if recovery else ROOT
            )
            trusted = Workspace(
                output / "trusted", recovery["prior"] / "trusted" if recovery else ROOT
            )
        if recovery:
            state["recovery"] = {
                "prior_campaign": str(recovery["prior"]),
                "pending_request": str(recovery["request"]),
                "method": "retry_saved_conversation_without_action_replay",
            }
            save(output / "recovery.json", state["recovery"])
        adapter = adapter_factory(
            {
                **config["adapter"],
                "hermes_root": str(source),
                "source_revision": config["source"].get("revision"),
                "target": config["target"],
            }
        )
        workspace.runtime_kind = (
            config["adapter"].get("kind")
            if config["adapter"].get("kind") in {"openhands", "deerflow"}
            else "hermes"
        )
        seed = None
        if config.get("seed_scenarios") and not (recovery or resumed):
            seed = workspace.seed(config["seed_scenarios"], limits["active_tests"])
            seed["prior_evidence"] = []
            for name, expected in config["seed_scenarios"].get("evidence", {}).items():
                path = Path(name)
                if path.is_symlink() or sha256_file(path) != expected:
                    raise ValueError(f"Saved campaign evidence changed: {path.name}")
                eid = evidence.add(path)
                seed["prior_evidence"].append({"id": eid, **evidence.items[eid]})
            save(output / "seed_scenarios.json", seed)
        catalog_evidence = None
        if execute and hasattr(adapter, "inspect_tool_catalog"):
            catalog = adapter.inspect_tool_catalog(output / "native_catalog", trusted.root)
            save(output / "native_tool_catalog.json", catalog)
            catalog_id = evidence.add(output / "native_tool_catalog.json")
            catalog_evidence = {"id": catalog_id, **evidence.items[catalog_id]}
            if catalog.get("available") and catalog.get("physical_workspace"):
                workspace.artifact_runtime = {
                    "image": catalog["runtime_image"],
                    "workspace": catalog["physical_workspace"],
                    "visible_workspace": catalog["workspace"],
                }
        client = client_factory(
            config["orchestrator"], output, redactor, orch_key, limits.get("requests")
        )
        previous = (
            json.loads(Path(state["rounds"][-2]["analysis"]).read_text(encoding="utf-8"))
            if resumed and len(state["rounds"]) > 1
            else None
        )
        for index in range(resumed["round"] if resumed else 1, limits["rounds"] + 1):
            directory = output / f"round_{index:03d}"
            directory.mkdir()
            continuing = resumed is not None and index == resumed["round"]
            record = (
                state["rounds"][-1]
                if continuing
                else {"round": index, "generations": [], "executions": {}}
            )
            if not continuing:
                state["rounds"].append(record)
            feedback = resumed["feedback"] if continuing else []
            conversations = {role: [] for role in ("main", *REVIEWERS)}
            if continuing:
                conversations["main"] = resumed["messages"]
            if recovery and index == 1:
                conversations["main"] = recovery["messages"]
            limit = (
                limits["initial_tests"]
                if index == 1 and not config.get("seed_scenarios")
                else limits["active_tests"]
            )
            workspace.active_limit = limit
            for generation in range(
                resumed["generation"] if continuing else 1, limits["generations"] + 1
            ):
                print(f"Round {index}, generation {generation}", flush=True)
                progress("generation", round=index, generation=generation)
                stage = directory / f"generation_{generation:02d}"
                stage.mkdir()
                plan = exchange(
                    client,
                    evidence,
                    {
                        "task": SEEDED_GENERATION_TASK
                        if config.get("seed_scenarios")
                        else GENERATION_TASK,
                        "seed_suite": seed,
                        "seed_guidance": SEED_GUIDANCE if config.get("seed_scenarios") else None,
                        **(
                            variant_context(variant, "main")
                            if not conversations["main"]
                            or (continuing and generation == resumed["generation"])
                            else {}
                        ),
                        "scope": config["scope"],
                        "requirements": DESIGN_REQUIREMENTS,
                        "round": index,
                        "generation": generation,
                        "max_active_tests": limit,
                        "execution_enabled": execute,
                        "schema": adapter.schema,
                        "files": workspace.inventory(),
                        "source_index": source_id,
                        "native_tool_catalog": catalog_evidence,
                        "source_repository": config["source"],
                        "runtime_contract": adapter.describe()
                        if hasattr(adapter, "describe")
                        else {"framework": config["adapter"].get("kind", "custom")},
                        "campaign_recovery": state.get("recovery"),
                        "version_change": version_request,
                        "review_feedback": feedback,
                        "previous_analysis": previous,
                        "prior_rounds": [
                            {
                                "round": r["round"],
                                "active_ids": r.get("active_ids", []),
                                "analysis_evidence_id": r["analysis_evidence_id"],
                            }
                            for r in state["rounds"]
                            if "analysis_evidence_id" in r
                        ],
                    },
                    workspace=workspace,
                    source=source,
                    journal=stage / "actions.jsonl",
                    conversation=conversations["main"],
                    continue_pending=bool(recovery and index == 1 and generation == 1),
                    response_validator=lambda value: (
                        validate_proposal(value, evidence, source),
                        validate_design(value, workspace.validate(limit))
                        if variant != "baseline"
                        else None,
                        validate_version_plan(value, change["changed_files"])
                        if version_mode
                        else None,
                    ),
                )
                save(stage / "proposal.json", plan)
                validation = {"passed": False}
                progress("preflight", round=index, generation=generation)
                cases = []
                review_inputs = workspace.review_scenarios()
                try:
                    cases = workspace.validate(limit)
                    rows = plan.get("rationale", [])
                    if {r["id"] for r in rows} != {s["id"] for s in cases}:
                        raise ValueError("Rationale must cover the active suite")
                    hashes = workspace.snapshot(stage / "candidate")
                    save(stage / "hashes.json", hashes)
                    validation = (
                        adapter.preflight(stage / "candidate", stage / "validation")
                        if execute
                        else {
                            "passed": True,
                            "scope": "schema_and_python_syntax",
                            "runtime_validation": "requires_execute",
                        }
                    )
                except (ValueError, KeyError, OSError, SyntaxError) as exc:
                    validation = {"passed": False, "error": str(exc)}
                validation = validation_evidence(validation, stage / "validation", evidence)
                save(stage / "validation.json", validation)
                oracle_validation = workspace.oracle_validation(trusted.root, validation)
                reviews = {}
                if generation < limits["generations"]:
                    for role, responsibility in REVIEWERS.items():
                        progress("review", round=index, generation=generation, role=role)
                        print(
                            f"Round {index}, generation {generation}, reviewer {role}", flush=True
                        )
                        review = exchange(
                            client,
                            evidence,
                            {
                                "task": review_task(role, responsibility),
                                **(
                                    variant_context(variant, role)
                                    if not conversations[role]
                                    else {}
                                ),
                                "requirements": DESIGN_REQUIREMENTS,
                                "scope": config["scope"],
                                "max_active_tests": limit,
                                "proposal": plan,
                                "architecture_evidence": {
                                    ref: evidence.items[ref.split(":")[0]]
                                    for row in plan["rationale"]
                                    for ref in row.get("architecture_refs", [])
                                },
                                "scenarios": review_inputs["scenarios"],
                                "unreadable_scenario_files": review_inputs["errors"],
                                "evaluator_changes": workspace.evaluator_changes(trusted.root),
                                "files": workspace.inventory(),
                                "mechanical_validation": validation,
                                "oracle_validation": oracle_validation,
                                "campaign_recovery": state.get("recovery"),
                                "previous_findings": feedback,
                                "source_index": source_id,
                                "native_tool_catalog": catalog_evidence,
                                "runtime_contract": adapter.describe()
                                if hasattr(adapter, "describe")
                                else {"framework": config["adapter"].get("kind", "custom")},
                            },
                            workspace=workspace,
                            readonly=True,
                            source=source,
                            journal=stage / f"{role}_reads.jsonl",
                            conversation=conversations[role],
                            response_validator=lambda value, role=role: (
                                validate_review(
                                    value,
                                    role,
                                    [
                                        {"id": p.stem}
                                        for p in (workspace.root / "scenarios").glob("*.json")
                                    ],
                                    oracle_validation=oracle_validation,
                                ),
                                validate_assessments(value, review_inputs["scenarios"])
                                if variant != "baseline"
                                else None,
                            ),
                        )
                        save(stage / f"review_{role}.json", review)
                        reviews[role] = review
                        print(
                            f"{role}: accepted={review['accepted']}, issues={len(review['issues'])}",
                            flush=True,
                        )
                record["generations"].append(
                    {
                        "generation": generation,
                        "validation": validation,
                        "reviews": reviews,
                        "proposal": str(stage / "proposal.json"),
                    }
                )
                if reviews:
                    feedback = [{"role": r, **v} for r, v in reviews.items()]
                else:
                    feedback = [f for f in feedback if f.get("role") != "mechanical_validation"]
                if not validation["passed"]:
                    feedback.append({"role": "mechanical_validation", **validation})
                save(stage / "checkpoint.json", state)
                if (
                    validation["passed"]
                    and reviews
                    and all(r["accepted"] for r in reviews.values())
                ):
                    break
            if not validation["passed"]:
                try:
                    cases = workspace.validate(limit, check_code=False)
                except (ValueError, KeyError, OSError) as exc:
                    state["stop_reason"] = "invalid_final_scenario_contract"
                    record["error"] = str(exc)
                    break
                if not (stage / "candidate").exists():
                    save(stage / "hashes.json", workspace.snapshot(stage / "candidate"))
            record["approval"] = (
                "reviewers_accepted"
                if reviews and all(r["accepted"] for r in reviews.values())
                else "review_budget_exhausted"
            )
            record["unresolved_review_findings"] = feedback
            record["active_ids"] = [s["id"] for s in cases]
            candidate = stage / "candidate"
            if not execute:
                state["stop_reason"] = "generated_only"
                break
            # All lanes receive byte-identical scenario files and separate evaluator copies.
            # In version mode the base_version lane reruns the original evaluator against the
            # base framework revision, so trusted vs base_version isolates the framework change.
            lanes = [("candidate", candidate, adapter), ("trusted", trusted.root, adapter)]
            if version_mode:
                lanes.append(("base_version", trusted.root, base_adapter))
            for lane, evaluator, lane_adapter in lanes:
                progress("execution", round=index, lane=lane)
                print(
                    f"Round {index}, executing {len(cases)} tests with {lane} evaluator", flush=True
                )
                lane_root = directory / lane
                lane_root.mkdir()
                shutil.copytree(
                    evaluator,
                    lane_root / "evaluator",
                    ignore=shutil.ignore_patterns("__pycache__", "scenarios"),
                )
                shutil.copytree(candidate / "scenarios", lane_root / "evaluator/scenarios")
                result = lane_adapter.execute_revision(
                    lane_root / "evaluator", lane_root / "execution", config["target"], target_key
                )
                report_path = lane_root / "execution/report.json"
                if report_path.exists() and "cases" in json.loads(
                    report_path.read_text(encoding="utf-8")
                ):
                    try:
                        audit = audit_campaign(report_path)
                        result["evidence_audit"] = {
                            "passed": audit["passed"],
                            "errors": audit["errors"],
                            "measured_phases": len(audit["phases"]),
                            "measurements": audit["cost_recomputed_once_per_phase"],
                        }
                    except (ValueError, KeyError, OSError, TypeError) as exc:
                        result["evidence_audit"] = {"passed": False, "error": str(exc)}
                record["executions"][lane] = result
                print(f"{lane}: {result['status']}", flush=True)
                save(lane_root / "execution_result.json", result)
                if version_mode and lane in {"trusted", "base_version"}:
                    _index_lane(
                        index_path,
                        report_path,
                        change["target_revision" if lane == "trusted" else "base_revision"],
                        lane=lane,
                        campaign=str(output),
                        round=index,
                    )
            differential = None
            if version_mode:
                differential = _version_differential(directory, plan, record, change, evidence)
            transcript_ids = []
            raw_ids = []
            for lane in record["executions"]:
                lane_output = directory / lane / "execution"
                raw_ids.append(evidence.add(directory / lane / "execution_result.json"))
                exported = export_execution(lane_output)
                for transcript in exported:
                    transcript_ids.append(evidence.add(transcript))
                for location in (lane_output, directory / lane / "preflight"):
                    for path in sorted(location.rglob("*")):
                        if (
                            path.is_file()
                            and path.suffix in {".json", ".jsonl", ".log", ".txt", ".md", ".py"}
                            and path not in exported
                        ):
                            raw_ids.append(evidence.add(path))
                log = directory / lane / "execution.log"
                if log.exists():
                    raw_ids.append(evidence.add(log))
            # Send cleaned transcripts automatically. Raw files remain addressable by ID.
            transcripts = [
                evidence.read({"id": eid, "offset": offset, "length": 40000})
                for eid in transcript_ids
                for offset in range(
                    0, len(Path(evidence.items[eid]["path"]).read_text(encoding="utf-8")), 40000
                )
            ]
            progress("analysis", round=index)
            analysis = exchange(
                client,
                evidence,
                {
                    "task": ANALYSIS_TASK,
                    **variant_context(variant, "analysis"),
                    "analysis_requirements": ANALYSIS_REQUIREMENTS,
                    "scenarios": cases,
                    "scope": config["scope"],
                    "round": index,
                    "max_rounds": limits["rounds"],
                    "max_active_tests": limits["active_tests"],
                    "executions": record["executions"],
                    "usage_before_this_analysis": {
                        "orchestrator": api_usage(output),
                        "target": target_api_usage(output),
                    },
                    "evaluator_changes": workspace.evaluator_changes(trusted.root),
                    "transcripts": transcripts,
                    "campaign_recovery": state.get("recovery"),
                    "raw_evidence": {eid: evidence.items[eid] for eid in raw_ids},
                    "review_findings": record["unresolved_review_findings"],
                    "version_change": version_request,
                    "version_differential": differential,
                    "version_analysis_guidance": VERSION_ANALYSIS_GUIDANCE
                    if version_mode
                    else None,
                },
                workspace=workspace,
                readonly=True,
                source=source,
                journal=directory / "analysis_reads.jsonl",
                response_validator=lambda value: (
                    validate_analysis(
                        value,
                        evidence,
                        record["active_ids"],
                        strict_owners=True,
                        executions=record["executions"],
                    ),
                    validate_stopping(value) if variant != "baseline" else None,
                ),
            )
            current = set(transcript_ids + raw_ids)
            if any(
                not any(ref.split(":")[0] in current for ref in row["evidence_refs"])
                for row in analysis["scenarios"]
            ):
                raise ValueError("Analysis requires current execution citations for every test")
            if type(analysis.get("converged")) is not bool or not analysis.get(
                "convergence_reason"
            ):
                raise ValueError("Explicit convergence decision and reason required")
            if analysis["converged"] and analysis.get("proposed_changes"):
                raise ValueError("Convergence conflicts with proposed changes")
            save(directory / "analysis.json", analysis)
            record["analysis_evidence_id"] = evidence.add(directory / "analysis.json")
            previous = analysis
            record["analysis"] = str(directory / "analysis.json")
            save(directory / "checkpoint.json", state)
            if analysis["converged"]:
                state["stop_reason"] = "orchestrator_converged"
                break
        else:
            state["stop_reason"] = "execution_round_limit"
    except CampaignPreempted as exc:
        state.update(stop_reason="operator_preempted", preemption_reason=str(exc))
    except BaseException as exc:
        state["error"] = redactor(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        write_json_atomic(
            output / "progress.json",
            {
                "phase": "terminal",
                "stop_reason": state["stop_reason"],
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        save(
            output / "orchestrator_usage.json",
            {
                **api_usage(output),
                "target_api_usage": target_api_usage(output),
                "scope": "Top-level request/token/role fields describe orchestrator calls only. target_api_usage separately includes target preflight, task and auxiliary calls under this campaign directory.",
            },
        )
        save(output / "evidence_index.json", evidence.items)
        write_json_atomic(output / "campaign.json", redactor(state))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--execute", action="store_true", help="Run generated agent tasks locally; not sandboxed"
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="One active test and one execution round, including both evaluator lanes",
    )
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.smoke:
        config["limits"] = {
            **config.get("limits", {}),
            "rounds": 1,
            "initial_tests": 1,
            "active_tests": 1,
        }
        config["scope"] += SMOKE_SCOPE
    output = run(config, execute=args.execute)
    state = json.loads((output / "campaign.json").read_text(encoding="utf-8"))
    return 2 if state["stop_reason"] in {"error", "invalid_final_scenario_contract"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
