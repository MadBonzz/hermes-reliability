"""Passive observation of selected native Hermes checks, including allowed calls.

Wrap existing loaded call sites temporarily; invoke each original exactly once.
No policy is added, no guard is re-run, and no installed source file is changed.
Missing hooks are explicit coverage gaps, never evidence of missing enforcement.
"""

import functools
import inspect
import os
import sys
import threading
import uuid
from contextvars import ContextVar
from pathlib import Path

from .runtime_support import sha256_file

_LOCK = threading.Lock()
HOOKS = [
    ("tools.file_tools", name, control, kind)
    for name, control, kind in [
        ("_stale_overwrite_blocker", "whole_file_overwrite", "refusal_reason"),
        ("_check_sensitive_path", "sensitive_path_write", "refusal_reason"),
        ("_check_binary_document_write", "binary_document_write", "refusal_reason"),
        ("_check_protected_instruction_write", "protected_instruction_write", "refusal_reason"),
        ("_check_approval_required_write", "configured_write_approval", "refusal_reason"),
        ("_check_cross_profile_path", "backend_path_routing", "refusal_reason"),
        ("_is_internal_file_tool_content", "display_text_write", "refuse_if_true"),
        ("_file_version", "file_version_snapshot", "measurement"),
    ]
] + [
    (
        "tools.file_tools_write_guards",
        "_has_full_write_baseline",
        "full_write_baseline",
        "predicate",
    ),
    ("tools.file_tools_write_guards", "_read_mtime_drifted", "read_mtime_drift", "predicate"),
    ("tools.file_tools_read_tracking", "_file_version", "file_version_snapshot", "measurement"),
    ("tools.file_state", "guard_disabled", "file_state_guard_disabled", "predicate"),
    ("tools.file_state", "check_stale", "file_state_staleness", "refusal_reason"),
]
PATH_FIELDS = {"filepath", "path", "paths", "resolved", "resolved_or_path", "task_id"}


def evidence_value(value):
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [evidence_value(v) for v in value]
    return value


class NativeControlObserver:
    def __init__(self, modules=None, hooks=None):
        self.modules = sys.modules if modules is None else modules
        self.hooks = HOOKS if hooks is None else hooks
        self.originals = []
        self.context = ContextVar("native_control_call", default=None)
        self.tokens = {}
        self.errors = []
        self.active = False
        self.manifest = {
            "version": 1,
            "mode": "passive_native_callsite_observation",
            "instrumented": [],
            "unavailable": [],
            "source_sha256": {},
            "environment_switches": {
                key: os.environ.get(key)
                for key in (
                    "HERMES_AUTO_APPROVE",
                    "HERMES_NON_INTERACTIVE",
                    "HERMES_DISABLE_FILE_STATE_GUARD",
                )
            },
            "limitations": "Selected in-process file controls only. A missing event is not proof of absent enforcement. Allows include non-applicable/disabled checks. Terminal/subprocess/internal paths may bypass these call sites.",
        }

    def start_call(self, sink, call_id, tool):
        key = threading.get_ident(), call_id
        self.tokens[key] = self.context.set({"sink": sink, "call_id": call_id, "tool": tool})

    def end_call(self, call_id):
        token = self.tokens.pop((threading.get_ident(), call_id), None)
        if token is not None:
            self.context.reset(token)

    def emit(self, context, event, **fields):
        try:
            context["sink"](
                event,
                call_id=context["call_id"],
                tool=context["tool"],
                owner="hermes_native",
                **fields,
            )
        except Exception as exc:
            # Logging must not change the native allow/refuse decision. The runner
            # separately fails the evaluation when observer evidence cannot be saved.
            self.errors.append(f"Native control logging failed: {type(exc).__name__}: {exc}")

    def wrap(self, original, hook, control, kind):
        signature = inspect.signature(original)

        @functools.wraps(original)
        def observed(*args, **kwargs):
            context = self.context.get()
            if not self.active or context is None:
                return original(*args, **kwargs)
            invocation = uuid.uuid4().hex
            try:
                bound = signature.bind_partial(*args, **kwargs).arguments
                inputs = {k: evidence_value(v) for k, v in bound.items() if k in PATH_FIELDS}
            except TypeError:
                inputs = {"binding_unavailable": True}
            common = {
                "invocation_id": invocation,
                "hook": hook,
                "control": control,
                "result_semantics": kind,
            }
            self.emit(context, "native_control_start", **common, inputs=inputs)
            try:
                result = original(*args, **kwargs)
            except BaseException as exc:
                self.emit(
                    context, "native_control_exception", **common, exception_type=type(exc).__name__
                )
                raise
            disposition = "value_observed"
            if kind in {"refusal_reason", "refuse_if_true"}:
                disposition = "refusal_returned" if result else "no_refusal_returned"
            self.emit(
                context,
                "native_control_result",
                **common,
                result=evidence_value(result),
                disposition=disposition,
            )
            return result

        return observed

    def __enter__(self):
        if not _LOCK.acquire(blocking=False):
            raise RuntimeError("Concurrent native control observation is unsupported")
        try:
            for module_name, name, control, kind in self.hooks:
                hook = module_name + "." + name
                module = self.modules.get(module_name)
                original = getattr(module, name, None) if module else None
                if not callable(original):
                    self.manifest["unavailable"].append(hook)
                    continue
                replacement = self.wrap(original, hook, control, kind)
                self.originals.append((module, name, original))
                setattr(module, name, replacement)
                self.manifest["instrumented"].append(hook)
                source = inspect.getsourcefile(original)
                if source and Path(source).is_file():
                    self.manifest["source_sha256"][source] = sha256_file(source)
            self.active = True
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, *exc):
        self.active = False
        try:
            for module, name, original in reversed(self.originals):
                setattr(module, name, original)
            self.originals.clear()
            self.tokens.clear()
        finally:
            _LOCK.release()


def control_coverage(executions, events):
    """Describe observed routes, including successes; never infer missing policy."""
    rows = []
    for call in executions:
        cid = call.get("id")
        native = [
            e for e in events if e.get("call_id") == cid and e["event"] == "native_control_result"
        ]
        before = next(
            (
                e.get("workspace", {})
                for e in events
                if e.get("id") == cid and e["event"] == "tool_state_before"
            ),
            None,
        )
        after = next(
            (
                e.get("workspace", {})
                for e in events
                if e.get("id") == cid and e["event"] == "tool_state_after"
            ),
            None,
        )
        changed = None
        if before is not None and after is not None:
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        rows.append(
            {
                "call_id": cid,
                "tool": call["name"],
                "changed_workspace_paths": changed,
                "native_checks": [
                    {
                        "control": e["control"],
                        "disposition": e["disposition"],
                        "result": e["result"],
                        "invocation_id": e["invocation_id"],
                    }
                    for e in native
                ],
                "observation": "native_checks_observed"
                if native
                else "no_instrumented_check_observed",
                "enforcement_failure": "not_inferred",
                "interpretation": "A safe action without a refusal does not test rejection. Missing events may mean a different route, unavailable instrumentation, or inapplicability; inspect the coverage manifest and runtime contract.",
            }
        )
    return rows
