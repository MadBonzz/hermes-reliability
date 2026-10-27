"""Shared configuration, Hermes initialization, and data-access helpers.

Import this module as src.utils so it cannot shadow Hermes's own utils module.
Hermes itself is imported only when load_hermes() is called.
"""

import copy
import importlib
import inspect
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = None
model_tools = toolsets = registry = AIAgent = None


def load_eval_config(env_path):
    """Read only the requested .env; shell variables win without mutating them."""
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        raise RuntimeError(
            "Run uv sync at the repository root; for an activated Hermes environment, use uv sync --active --inexact"
        ) from exc
    values = dotenv_values(env_path, encoding="utf-8-sig", interpolate=False)
    config = {}
    for layer in (values, os.environ):
        normalized = {key: value for key, value in layer.items() if value is not None}
        # Normalize each layer before merging so even a legacy shell variable
        # overrides a generic .env value. Generic names win within one layer.
        for legacy, generic in (
            ("LM_STUDIO_URL", "EVAL_BASE_URL"),
            ("LM_STUDIO_API_KEY", "EVAL_API_KEY"),
        ):
            if legacy in normalized:
                normalized.setdefault(generic, normalized[legacy])
        config.update(normalized)
    return config


def load_hermes(root: str | None = None) -> Path:
    """Resolve CLI/environment overrides BEFORE importing any Hermes modules."""
    global REPO_ROOT, model_tools, toolsets, registry, AIAgent
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
    selected = next((p.resolve() for p in candidates if (p / "run_agent.py").is_file()), None)
    if selected is None:
        raise RuntimeError("Hermes checkout not found; set --hermes-root or HERMES_AGENT_ROOT")
    if REPO_ROOT is not None:
        if REPO_ROOT != selected:
            raise RuntimeError("Cannot switch Hermes checkouts within an imported process")
        return REPO_ROOT
    sys.path.insert(0, str(selected))
    os.environ["HERMES_AUTO_APPROVE"] = "1"
    os.environ["HERMES_NON_INTERACTIVE"] = "1"
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
    agent = AIAgent(**{k: v for k, v in candidates.items() if k in accepted and v is not None})
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


def resolve_ws_path(workspace: Path, rel_path: str) -> Path:
    """Safely resolves a relative path inside the scenario workspace."""
    workspace = workspace.resolve()
    clean = rel_path.replace("{WORKSPACE}", str(workspace)).replace("\\", "/")
    target = (workspace / clean).resolve()
    if not target.is_relative_to(workspace):
        raise ValueError(f"Path escapes scenario workspace: {rel_path!r}")
    return target
