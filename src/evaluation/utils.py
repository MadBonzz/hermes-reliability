"""Shared configuration, Hermes initialization, and data-access helpers.

Import this module as src.evaluation.utils so it cannot shadow Hermes's own utils module.
Hermes itself is imported only when load_hermes() is called.
"""

import copy
import importlib
import inspect
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = None
model_tools = toolsets = registry = AIAgent = None

EVAL_HERMES_CONFIG = {"agent": {"coding_context": "off", "coding_instructions": ""}}


@contextmanager
def hermes_run_environment(home):
    """Use a fresh, recorded Hermes profile instead of the operator's ambient config.

    Enter before importing Hermes: its modules cache home/config paths at import.
    Native context-file skipping alone does not disable automatic repo discovery.
    """
    home = Path(home).resolve()
    home.mkdir(parents=True, exist_ok=False)
    (home / "config.yaml").write_text(json.dumps(EVAL_HERMES_CONFIG, indent=2), encoding="utf-8")
    previous = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        yield home
    finally:
        if previous is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous


def load_eval_config(env_path=None):
    """Target-worker compatibility: environment only; Hermes owns secret files."""
    config = dict(os.environ)
    for legacy, generic in (("LM_STUDIO_URL", "EVAL_BASE_URL"), ("LM_STUDIO_API_KEY", "EVAL_API_KEY")):
        if legacy in config:
            config.setdefault(generic, config[legacy])
    return config


HERMES_APPROVAL_MODES = ("auto", "gate")


def find_hermes_root(root: str | None = None) -> Path | None:
    """Locate the Hermes checkout without importing it (None when absent)."""
    explicit = root or os.environ.get("HERMES_AGENT_ROOT")
    candidates = (
        [Path(explicit)]
        if explicit
        else [
            PROJECT_ROOT,
            Path(sys.prefix).parent / "workspace",
            Path.home() / "AppData/Local/hermes/hermes-agent",
        ]
    )
    return next((p.resolve() for p in candidates if (p / "run_agent.py").is_file()), None)


def apply_hermes_approval(mode: str = "auto") -> None:
    """Set Hermes's approval environment for unattended runs.

    auto: HERMES_AUTO_APPROVE=1, so Hermes approves actions it would normally
    ask about. Its approval gate is NOT exercised (historical default).
    gate: auto-approve is left unset, so Hermes's own approval gate decides.
    Both keep HERMES_NON_INTERACTIVE=1 so a run never waits for a human.
    """
    if mode not in HERMES_APPROVAL_MODES:
        raise ValueError(f"Unknown Hermes approval mode: {mode!r}")
    os.environ["HERMES_NON_INTERACTIVE"] = "1"
    if mode == "auto":
        os.environ["HERMES_AUTO_APPROVE"] = "1"
    else:
        os.environ.pop("HERMES_AUTO_APPROVE", None)


def load_hermes(root: str | None = None, approval: str = "auto") -> Path:
    """Resolve CLI/environment overrides BEFORE importing any Hermes modules."""
    global REPO_ROOT, model_tools, toolsets, registry, AIAgent
    selected = find_hermes_root(root)
    if selected is None:
        raise RuntimeError("Hermes checkout not found; set --hermes-root or HERMES_AGENT_ROOT")
    if REPO_ROOT is not None:
        if REPO_ROOT != selected:
            raise RuntimeError("Cannot switch Hermes checkouts within an imported process")
        return REPO_ROOT
    sys.path.insert(0, str(selected))
    apply_hermes_approval(approval)
    model_tools = importlib.import_module("model_tools")
    toolsets = importlib.import_module("toolsets")
    registry = importlib.import_module("tools.registry").registry
    AIAgent = importlib.import_module("run_agent").AIAgent
    REPO_ROOT = selected
    return selected


def get_json_path(data: Any, path_str: str) -> tuple[bool, Any]:
    """Resolves a dot-separated path (e.g. 'contact.primary_email' or '0.booking_id') in JSON."""
    if not path_str:
        return True, data
    curr = data
    for part in path_str.split("."):
        if isinstance(curr, dict) and part in curr:
            curr = curr[part]
        elif isinstance(curr, list) and part.isdigit() and int(part) < len(curr):
            curr = curr[int(part)]
        else:
            return False, None
    return True, curr


def unwrap_tool_call(name, arguments):
    """Resolve Hermes's single-target bridge, keeping malformed envelopes intact.

    Native Hermes expands local batches before persisting history. Do not parse
    shell/code text or guess dispatches from a multi-target/invalid envelope.
    """
    original = name, arguments
    if name != "tool_call":
        return original
    try:
        envelope = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(envelope, dict):
            return original
        entries = envelope.get("calls")
        if entries is None:
            entries = [envelope]
        if isinstance(entries, str):
            entries = json.loads(entries)
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            return original
        entry = entries[0]
        target = entry.get("name")
        if not isinstance(target, str) or not target.strip():
            return original
        target = target.strip()
        if target in {"tool_call", "tool_describe", "tool_search"}:
            return original
        args = entry.get("arguments")
        if args is None or (isinstance(args, str) and not args.strip()):
            args = {}
        if isinstance(args, str):
            args = json.loads(args)
        if not isinstance(args, dict):
            return original
        return target, args
    except (TypeError, ValueError):
        return original


def extract_turn_tool_calls(messages: list[dict[str, Any]], start_idx: int) -> list[dict[str, Any]]:
    """
    Extracts all tool calls emitted by the assistant during the current turn.
    Handles both OpenAI dict format (`tc["function"]["name"]`) and object format.
    """
    calls = []
    for msg in messages[start_idx:]:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    name = fn.get("name") or tc.get("name", "unknown")
                    args = fn.get("arguments") if "arguments" in fn else tc.get("arguments", "{}")
                else:
                    fn = getattr(tc, "function", None)
                    name = getattr(fn, "name", "unknown") if fn else "unknown"
                    args = getattr(fn, "arguments", "{}") if fn else "{}"
                name, args = unwrap_tool_call(name, args)
                calls.append({"name": name, "arguments": args})
    return calls


@contextmanager
def hermes_workspace(workspace, task_id):
    """Bind prompt and real file/terminal tools to an isolated scenario environment.

    Constructor cwd alone does not configure Hermes's tool backend. A cwd-only
    override also shares the default environment cache; env_type gives this
    benchmark task its own cache identity. Keep the identity across all turns.
    """
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd
    from tools.terminal_tool import clear_task_env_overrides, register_task_env_overrides
    from tools.terminal_tool_lifecycle import cleanup_vm

    token = set_session_cwd(str(workspace))
    try:
        register_task_env_overrides(task_id, {"cwd": str(workspace), "env_type": "local"})
        yield
    finally:
        try:
            cleanup_vm(task_id)
        finally:
            try:
                clear_task_env_overrides(task_id)
            finally:
                reset_session_cwd(token)


def instantiate_hermes_agent(base_url, model_name, api_key, max_iterations, workspace=None):
    """Use the same chat-completions transport for local and hosted compatible APIs."""
    if AIAgent is None:
        raise RuntimeError("Call load_hermes before creating agents")
    accepted = inspect.signature(AIAgent.__init__).parameters
    candidates = {
        "model": model_name,
        "provider": "custom",
        "base_url": base_url,
        "api_key": api_key,
        "api_mode": "chat_completions",
        "max_iterations": max_iterations,
        "quiet_mode": True,
        "persist_session": False,
        "skip_memory": True,
        "skip_context_files": True,
        "skip_background_review": True,
        "load_soul_identity": False,
        "cwd": str(workspace) if workspace else None,
    }
    if model_name == "command-a-plus-05-2026":
        candidates["reasoning_config"] = {"enabled": True, "effort": "high"}
    elif model_name == "command-a-03-2025":
        # The compatibility endpoint rejects explicit effort levels for Command A.
        candidates["reasoning_config"] = {}
    agent = AIAgent(**{k: v for k, v in candidates.items() if k in accepted and v is not None})
    if model_name == "command-a-03-2025":
        agent.reasoning_config = {}
    # New Hermes versions use this flag instead of the removed persist_session kwarg.
    if hasattr(agent, "_persist_disabled"):
        agent._persist_disabled = True
    return agent


def check_tool_catalog(agent, mocks):
    tools = getattr(agent, "tools", [])
    names = [t.get("function", {}).get("name") for t in tools]
    required = {"terminal", "read_file", "write_file", *mocks}
    valid = getattr(agent, "valid_tool_names", set())
    missing = required - set(names) | required - set(valid)
    if missing:
        raise RuntimeError(f"Missing required tool capabilities: {sorted(missing)}")
    if len(names) != len(set(names)):
        raise RuntimeError("Duplicate names in the model's tool catalog")
    for name in required:
        if name not in registry._tools:
            raise RuntimeError(f"Tool schema has no registered handler: {name}")
    return copy.deepcopy(tools)


def tool_working_directory(task_id):
    """Preserve native relative-path semantics after a task changes directories."""
    from tools.terminal_tool import get_session_cwd

    return get_session_cwd(task_id)


def resolve_ws_path(workspace: Path, rel_path: str) -> Path:
    """Safely resolves a relative path inside the scenario workspace."""
    workspace = workspace.resolve()
    clean = rel_path.replace("{WORKSPACE}", str(workspace)).replace("\\", "/")
    target = (workspace / clean).resolve()
    if not target.is_relative_to(workspace):
        raise ValueError(f"Path escapes scenario workspace: {rel_path!r}")
    return target
