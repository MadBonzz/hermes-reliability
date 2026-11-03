"""Durable traces, checkpoints, and filesystem evidence for the evaluator."""

import copy
import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path


def utc_now():
    return datetime.now(UTC).isoformat()


def json_safe(value):
    if hasattr(value, "model_dump"):
        return json_safe(value.model_dump())
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(json_safe(value), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(path)


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_jsonl(path):
    """Read complete JSONL evidence; malformed or empty records remain errors."""
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


class TurnTrace:
    """Capture execution events immediately, before history compaction can erase them."""

    def __init__(self, path=None, redact=None):
        self.path = Path(path) if path else None
        self.redact = redact or (lambda value: value)
        self.events = []
        self._lock = threading.Lock()

    def record(self, event, **fields):
        item = {"timestamp_utc": utc_now(), "event": event, **json_safe(fields)}
        with self._lock:
            self.events.append(item)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(self.redact(item), ensure_ascii=False) + "\n")

    def tool_start(self, call_id, name, arguments, *unused, **kwargs):
        self.record("tool_start", id=call_id, name=name, arguments=arguments)

    def tool_complete(self, call_id, name, arguments, result, *unused, **kwargs):
        self.record("tool_complete", id=call_id, name=name, arguments=arguments, result=result)

    def tool_calls(self):
        with self._lock:
            events = copy.deepcopy(self.events)
        completed = {e["id"]: e for e in events if e["event"] == "tool_complete"}
        return [
            dict(
                id=e["id"],
                name=e["name"],
                arguments=e["arguments"],
                result=completed.get(e["id"], {}).get("result"),
                completed=e["id"] in completed,
            )
            for e in events
            if e["event"] == "tool_start"
        ]


def current_turn_messages(messages, prompt):
    """Locate the actual user boundary, rather than relying on pre-compression length."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user":
            continue
        content = message.get("content")
        prefix = message.get("_merged_turn_prefix")
        # Hermes merges consecutive user messages after context truncation. Its
        # explicit metadata identifies the preserved prefix; do not use suffix
        # matching, which could mistake a quoted prompt for the actual boundary.
        merged = isinstance(prefix, str) and bool(prefix) and content == prefix + "\n\n" + prompt
        if content == prompt or merged:
            return messages[index:]
    return None


def workspace_manifest(workspace):
    """Snapshot small benchmark artifacts before cleanup, without following escaped links."""
    workspace = Path(workspace).resolve()
    # A missing directory and an empty one have different directory_files results.
    # Keep the root as well as nested directories so saved evidence retains that
    # distinction after the original workspace is removed.
    result = {".": {"kind": "directory"}} if workspace.is_dir() else {}
    for path in sorted(workspace.rglob("*")):
        if not path.resolve().is_relative_to(workspace) or path.is_symlink():
            continue
        if path.is_dir():
            result[path.relative_to(workspace).as_posix()] = {"kind": "directory"}
        elif path.is_file():
            raw = path.read_bytes()
            item = {"kind": "file", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            if len(raw) <= 1_000_000:
                try:
                    item["text"] = raw.decode("utf-8")
                except UnicodeDecodeError:
                    pass
            result[path.relative_to(workspace).as_posix()] = item
    return result
