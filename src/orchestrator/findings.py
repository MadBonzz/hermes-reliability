"""Cached, evidence-linked dashboard presentation using the campaign's orchestrator model."""

import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft7Validator, ValidationError

from ..evaluation.api_trace import SecretRedactor
from ..evaluation.runtime_support import write_json_atomic
from .dashboard import read_json
from .prompts import DASHBOARD_FINDINGS_PROMPT, DASHBOARD_FINDINGS_REPAIR

TEXT = {"type": "string", "minLength": 1}
PRESENTATION_SCHEMA = {
    "type": "object",
    "required": ["overview", "cards"],
    "additionalProperties": False,
    "properties": {
        "overview": TEXT,
        "cards": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "observation",
                    "causal_explanation",
                    "practical_implication",
                    "limitations",
                    "source_ids",
                ],
                "properties": {
                    **{
                        name: TEXT
                        for name in (
                            "title",
                            "observation",
                            "causal_explanation",
                            "practical_implication",
                        )
                    },
                    "limitations": {"type": "array", "items": TEXT},
                    "source_ids": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": TEXT,
                    },
                },
            },
        },
    },
}


def source_records(analysis):
    records = {}
    for row in analysis.get("scenarios", []):
        for index, finding in enumerate(row.get("findings") or [{}], 1):
            records[f"s{len(records) + 1}"] = {
                "kind": "scenario",
                "scenario_id": row["id"],
                "property_outcome": row.get("property_outcome", "uncertain"),
                "task_outcome": row.get("task_outcome"),
                "challenge_exposure": row.get("challenge_exposure"),
                "claim": finding.get("claim", "No specific causal finding was recorded."),
                "contributors": finding.get("contributors", []),
                "certainty": finding.get("certainty", "unresolved"),
                "evidence_refs": finding.get("evidence_refs", row.get("evidence_refs", [])),
                "analysis_location": f"scenarios/{row['id']}/findings/{index}",
            }
    for index, gap in enumerate(analysis.get("unresolved", []), 1):
        records[f"g{index}"] = {
            "kind": "evidence_gap",
            "claim": gap if isinstance(gap, str) else gap.get("description", json.dumps(gap)),
            "detail": gap,
            "certainty": "unresolved",
            "analysis_location": f"unresolved/{index}",
        }
    return records


def validate_presentation(value, sources):
    Draft7Validator(PRESENTATION_SCHEMA).validate(value)
    if not value["overview"].strip():
        raise ValueError("Overview must contain meaningful text")
    ids = [sid for card in value["cards"] for sid in card["source_ids"]]
    if set(ids) != set(sources) or len(ids) != len(set(ids)):
        raise ValueError("Every supplied source ID must occur exactly once across cards")
    for card in value["cards"]:
        if any(
            not card[name].strip()
            for name in ("title", "observation", "causal_explanation", "practical_implication")
        ):
            raise ValueError("Card fields must contain meaningful text")


def inputs(directory):
    directory = Path(directory)
    analysis = read_json(directory / "analysis.json")
    if not isinstance(analysis, dict) or not analysis.get("scenarios"):
        return None
    config = read_json(directory.parent / "config.json", {}).get("orchestrator", {})
    signature = json.dumps(
        [analysis, config, DASHBOARD_FINDINGS_PROMPT, PRESENTATION_SCHEMA], sort_keys=True
    ).encode()
    digest = hashlib.sha256(signature).hexdigest()
    return analysis, config, directory / "dashboard_findings" / digest


def snapshot(directory):
    item = inputs(directory)
    if item is None:
        return {"status": "awaiting_analysis"}
    analysis, config, folder = item
    result = read_json(folder / "presentation.json")
    if result is not None:
        return {
            "status": "ready",
            "model": config.get("model"),
            "presentation": result,
            "sources": source_records(analysis),
            "usage": read_json(folder / "status.json", {}).get("usage", {}),
        }
    return read_json(folder / "status.json", {"status": "pending", "model": config.get("model")})


@contextmanager
def exclusive(folder):
    """Prevent simultaneous dashboard servers from paying for the same presentation."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        try:
            yield True
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def generate(directory, environment, client_factory=None):
    from .runner import Client, api_usage

    item = inputs(directory)
    if item is None:
        return
    analysis, config, folder = item
    with exclusive(folder) as acquired:
        if not acquired or (folder / "presentation.json").exists():
            return
        if read_json(folder / "status.json", {}).get("status") == "error":
            return
        attempt = folder / uuid.uuid4().hex
        attempt.mkdir()
        key = environment.get(config.get("api_key_env", ""), "")
        redact = SecretRedactor([key])
        state = {
            "status": "generating",
            "model": config.get("model"),
            "attempt": attempt.name,
            "started_at": datetime.now(UTC).isoformat(),
        }
        write_json_atomic(folder / "status.json", state)
        try:
            if not key and client_factory is None:
                raise ValueError(
                    f"Set {config.get('api_key_env', 'the campaign API key')} to prepare findings"
                )
            sources = source_records(analysis)
            client = (client_factory or Client)(config, attempt, redact, key, None)
            messages = [
                {"role": "system", "content": DASHBOARD_FINDINGS_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "task": "Present dashboard findings.",
                                    "analysis": analysis,
                                    "sources": sources,
                                    "schema": PRESENTATION_SCHEMA,
                                }
                            ),
                        }
                    ],
                },
            ]
            for repair in range(3):
                value = None
                try:
                    value = client.ask(messages)
                    validate_presentation(value, sources)
                    break
                except (ValueError, ValidationError) as exc:
                    if repair == 2:
                        raise
                    if value is not None:
                        messages.append({"role": "assistant", "content": json.dumps(value)})
                    messages.append(
                        {"role": "user", "content": DASHBOARD_FINDINGS_REPAIR + str(exc)}
                    )
            # A mutable analysis cannot be paired with a presentation of earlier contents.
            if inputs(directory)[2] != folder:
                raise ValueError("Analysis changed during presentation generation")
            write_json_atomic(folder / "presentation.json", redact(value))
            state["status"] = "ready"
        except Exception as exc:
            state.update(status="error", error=redact(f"{type(exc).__name__}: {exc}"))
        finally:
            state["finished_at"] = datetime.now(UTC).isoformat()
            # Include earlier failed/interrupted attempts when the operator retries.
            usage = {
                "requests": 0,
                "reported_tokens": {"input_tokens": 0, "output_tokens": 0},
                "usage_missing": [],
                "by_role": {},
            }
            for prior in sorted(p for p in folder.iterdir() if p.is_dir()):
                measured = api_usage(prior)
                usage["requests"] += measured["requests"]
                for name, count in measured["reported_tokens"].items():
                    usage["reported_tokens"][name] += count
                usage["usage_missing"].extend(
                    f"{prior.name}/{name}" for name in measured["usage_missing"]
                )
                for role, counters in measured["by_role"].items():
                    target = usage["by_role"].setdefault(role, dict.fromkeys(counters, 0))
                    for name, count in counters.items():
                        target[name] += count
            state["usage"] = usage
            write_json_atomic(folder / "status.json", state)


class FindingsWorker:
    """Prepare each retained round once, including analyses from already-running controllers."""

    def __init__(self, catalog, *, credential_values, client_factory):
        self.catalog = catalog
        # Capture the owning profile's explicit values/client before the thread
        # starts. Background work must not consult the launch profile's environ.
        self.credential_values = dict(credential_values)
        self.client_factory = client_factory
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="dashboard-findings", daemon=True)

    def run(self):
        while not self.stop.is_set():
            try:
                for card in self.catalog.cards():
                    if card.get("bundled"):
                        continue
                    path = self.catalog.resolve(card["id"])
                    for analysis in sorted(path.glob("round_*/analysis.json"), reverse=True):
                        if self.stop.is_set():
                            return
                        generate(analysis.parent, self.credential_values, self.client_factory)
            except (OSError, ValueError) as exc:
                # Keep polling when a live file is replaced while discovering campaigns.
                print(f"Findings discovery will retry: {type(exc).__name__}", flush=True)
            self.stop.wait(10)
