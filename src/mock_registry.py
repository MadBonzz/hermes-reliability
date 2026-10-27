"""Scenario-local mock handlers and Hermes registry lifecycle.

Hermes dependencies are supplied by the caller after runtime discovery. Importing
this module does not load Hermes or mutate its registry.
"""

import copy
import inspect
import json
import time
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .utils import resolve_ws_path


def invalidate_tool_caches(registry, toolsets, model_tools) -> None:
    """Private registry restoration must invalidate Hermes's generation-keyed caches."""
    if hasattr(registry, "_generation"):
        registry._generation += 1
    clear = getattr(model_tools, "_clear_tool_defs_cache", None)
    if callable(clear):
        clear()
    memo = getattr(toolsets, "_resolve_toolset_memo", None)
    if isinstance(memo, dict):
        memo.clear()


class DynamicMockToolManager:
    """
    Registers declarative JSON mock tools into Hermes's ToolRegistry before a scenario,
    ensures AIAgent.tools and AIAgent.valid_tool_names include them,
    and restores the exact registry state after the scenario completes.
    """

    def __init__(
        self, workspace_dir: Path, fault_flags: dict[str, bool], *, registry, toolsets, model_tools
    ):
        self.registry = registry
        self.toolsets = toolsets
        self.model_tools = model_tools
        self.workspace_dir = workspace_dir
        self.fault_flags = fault_flags
        self.registered_schemas: dict[str, dict[str, Any]] = {}
        self._registry_tools_backup: dict[str, Any] = {}
        self._toolsets_backup: dict[str, Any] = {}
        self._registry_metadata_backup: dict[str, Any] = {}
        self._snapshot_taken = False

    def snapshot_registry(self) -> None:
        """Snapshots registry._tools and toolsets dicts prior to scenario registration."""
        if self._snapshot_taken:
            return
        if not isinstance(getattr(self.registry, "_tools", None), dict):
            raise RuntimeError("Unsupported Hermes registry: _tools dictionary missing")
        with getattr(self.registry, "_lock", nullcontext()):
            self._registry_tools_backup = copy.copy(self.registry._tools)
            for attr in ("_toolset_checks", "_toolset_aliases"):
                if isinstance(getattr(self.registry, attr, None), dict):
                    self._registry_metadata_backup[attr] = dict(getattr(self.registry, attr))

        for attr in ("TOOLSETS", "_HERMES_CORE_TOOLS", "CORE_TOOLS"):
            if hasattr(self.toolsets, attr):
                val = getattr(self.toolsets, attr)
                if isinstance(val, (dict, list, set)):
                    self._toolsets_backup[attr] = copy.deepcopy(val)
        self._snapshot_taken = True

    def _fault_response(self, behavior, *, clear=False):
        """Return the configured error only while its scenario-local flag is active."""
        flag = behavior.get("error_when_flag_set")
        if not flag or not self.fault_flags.get(flag):
            return None
        if clear and behavior.get("clear_flag_after_first_error", False):
            self.fault_flags[flag] = False
        return json.dumps(
            {
                "status": "error",
                "error": behavior.get("error_message", "500 Internal Tool Error"),
            }
        )

    def _mutate_state(self, behavior, target, args):
        """Apply a JSON mutation, preserving the configured before/after-write fault."""
        kind = behavior["type"]
        is_append = kind == "state_append_and_optional_error"
        is_race = kind == "unverified_race_deduct"
        fail_before = behavior.get("fail_before_write", not is_append)
        if not is_race and fail_before:
            error = self._fault_response(behavior, clear=True)
            if error is not None:
                return error

        if is_append:
            data = []
        elif is_race:
            data = {"balance": 100, "tx_count": 0}
        else:
            data = {}
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
                expected_type = list if is_append else dict
                if not isinstance(data, expected_type):
                    raise ValueError(
                        "expected a JSON array" if is_append else "expected a JSON object"
                    )
            except (OSError, ValueError) as exc:
                return json.dumps({"status": "error", "error": f"Invalid existing state: {exc}"})

        if is_append:
            data.append(args)
            result = {"status": "ok", "recorded": args, "total_items": len(data)}
        elif is_race:
            old_balance, old_count = float(data.get("balance", 100)), int(data.get("tx_count", 0))
            delay_ms = int(behavior.get("delay_ms", 0))
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)
            data["balance"] = old_balance - float(args.get("amount", 0))
            data["tx_count"] = old_count + 1
            result = {"status": "ok", "new_balance": data["balance"], "tx_count": data["tx_count"]}
        else:
            key = str(args.get(behavior.get("key_field", "key"), "default_key"))
            value = args.get(behavior.get("value_field", "value"), args)
            data[key] = value
            result = {"status": "ok", "updated": {key: value}}

        # Intentionally not atomic: the race mock must retain its read/write window.
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if is_append or (not is_race and not fail_before):
            error = self._fault_response(behavior, clear=True)
            if error is not None:
                return error
        return json.dumps(result)

    def _build_handler(self, tool_spec: dict[str, Any]) -> Callable:
        """Bind one declarative behavior to this scenario's workspace and flags."""
        behavior = tool_spec.get("behavior", {})
        kind = behavior.get("type", "static_return")
        mutation_types = {
            "state_append_and_optional_error",
            "state_kv_write_and_optional_error",
            "unverified_race_deduct",
        }
        if kind not in mutation_types | {"static_return"}:
            raise ValueError(f"Unknown mock behavior: {kind}")

        def handler(args: dict[str, Any], **kwargs) -> str:
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"raw": args}
            elif args is None:
                args = {}

            delay_ms = int(behavior.get("delay_ms", 0))
            if delay_ms > 0 and kind != "unverified_race_deduct":
                time.sleep(delay_ms / 1000.0)
            target = resolve_ws_path(
                self.workspace_dir, behavior.get("target_file", "mock_state.json")
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            if kind in mutation_types:
                return self._mutate_state(behavior, target, args)

            error = self._fault_response(behavior)
            if error is not None:
                return error
            if behavior.get("read_target_file", False) and target.exists():
                return target.read_text(encoding="utf-8")
            return json.dumps(behavior.get("return_payload", {"status": "ok"}))

        return handler

    def register_scenario_mocks(self, mock_specs: list[dict[str, Any]]) -> None:
        """
        Registers scenario-specific mock tools into `tools.registry.registry`.
        Note: In Hermes, `schema["description"]` is authoritative for the LLM.
        We also assign `toolset="terminal"` (an always-enabled core toolset) so
        `resolve_toolset()` never filters out the mock tool.
        """
        self.snapshot_registry()

        supports_override = "override" in inspect.signature(self.registry.register).parameters
        for spec in mock_specs:
            name = spec["name"]
            desc = spec.get("description", f"Mock tool {name}")
            schema = {
                "name": name,
                "description": desc,
                "parameters": spec.get("parameters", {"type": "object", "properties": {}}),
            }
            handler = self._build_handler(spec)

            reg_kwargs: dict[str, Any] = {
                "name": name,
                "toolset": "terminal",  # Piggyback on core 'terminal' toolset so it's always enabled
                "schema": schema,
                "handler": handler,
                "check_fn": lambda: True,
                "is_async": False,
                "description": desc,
            }
            if supports_override:
                reg_kwargs["override"] = True

            self.registry.register(**reg_kwargs)

            toolsets = getattr(self.toolsets, "TOOLSETS", {})
            if isinstance(toolsets, dict):
                for toolset_name in ("terminal", "hermes-cli", "core"):
                    entry = toolsets.get(toolset_name)
                    tools = entry.get("tools") if isinstance(entry, dict) else entry
                    if isinstance(tools, list) and name not in tools:
                        tools.append(name)

            self.registered_schemas[name] = schema
        invalidate_tool_caches(self.registry, self.toolsets, self.model_tools)

    def attach_to_agent_instance(self, agent: Any) -> None:
        """
        Hermes freezes `agent.tools` and `agent.valid_tool_names` inside `AIAgent.__init__`.
        This method guarantees that every registered mock tool is present in `agent.tools`
        and `agent.valid_tool_names` even if toolset filtering skipped it.
        """
        if not self.registered_schemas:
            return

        if hasattr(agent, "tools") and isinstance(agent.tools, list):
            existing_names = {
                t.get("function", {}).get("name") for t in agent.tools if isinstance(t, dict)
            }
            for name, schema in self.registered_schemas.items():
                if name not in existing_names:
                    agent.tools.append({"type": "function", "function": schema})

        if hasattr(agent, "valid_tool_names") and isinstance(agent.valid_tool_names, set):
            for name in self.registered_schemas:
                agent.valid_tool_names.add(name)

    def cleanup(self) -> None:
        """Strictly removes all registered mock tools and restores original registry & toolsets."""
        if not self._snapshot_taken:
            return
        with getattr(self.registry, "_lock", nullcontext()):
            self.registry._tools.clear()
            self.registry._tools.update(self._registry_tools_backup)
            for attr, backup in self._registry_metadata_backup.items():
                current = getattr(self.registry, attr)
                current.clear()
                current.update(backup)

        for attr, backup_val in self._toolsets_backup.items():
            if hasattr(self.toolsets, attr):
                current = getattr(self.toolsets, attr)
                if isinstance(current, dict):
                    current.clear()
                    current.update(copy.deepcopy(backup_val))
                elif isinstance(current, list):
                    current.clear()
                    current.extend(copy.deepcopy(backup_val))
                elif isinstance(current, set):
                    current.clear()
                    current.update(backup_val)

        invalidate_tool_caches(self.registry, self.toolsets, self.model_tools)
        self.registered_schemas.clear()
        self._snapshot_taken = False
