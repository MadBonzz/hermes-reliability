"""Offline check against the installed Hermes registry; does not invoke a model."""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import utils
from src.mock_registry import DynamicMockToolManager


def main():
    previous_home = os.environ.get("HERMES_HOME")
    with tempfile.TemporaryDirectory(prefix="hermes_integration_") as temporary:
        os.environ["HERMES_HOME"] = str(Path(temporary) / "home")
        try:
            return run_checks(Path(temporary))
        finally:
            if previous_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = previous_home


def run_checks(temporary_root):
    config = utils.load_eval_config(utils.PROJECT_ROOT / ".env")
    utils.load_hermes(config.get("HERMES_AGENT_ROOT"))
    original = dict(utils.registry._tools)
    specs = [
        {
            "name": "eval_probe",
            "description": "Offline integration probe",
            "parameters": {"type": "object", "properties": {}},
            "behavior": {"type": "static_return"},
        }
    ]
    print("Registered tools:", len(original))
    for mocks in (specs, [], specs):
        manager = DynamicMockToolManager(
            Path.cwd(),
            {},
            registry=utils.registry,
            toolsets=utils.toolsets,
            model_tools=utils.model_tools,
        )
        try:
            manager.snapshot_registry()
            manager.register_scenario_mocks(mocks)
            definitions = utils.model_tools.get_tool_definitions(
                enabled_toolsets=["terminal", "file"], quiet_mode=True
            )
            # The runtime may defer custom tools; the runner explicitly attaches mocks.
            agent = SimpleNamespace(
                tools=definitions, valid_tool_names={t["function"]["name"] for t in definitions}
            )
            manager.attach_to_agent_instance(agent)
            names = {t["function"]["name"] for t in agent.tools}
            assert {"terminal", "read_file", "write_file"} <= names, names
            assert ("eval_probe" in names) == bool(mocks), names
            if mocks:
                entry = utils.registry._tools["eval_probe"]
                assert entry.handler({}) == '{"status": "ok"}'
        finally:
            manager.cleanup()
        assert utils.registry._tools == original
        restored = {
            t["function"]["name"]
            for t in utils.model_tools.get_tool_definitions(
                enabled_toolsets=["terminal", "file"], quiet_mode=True
            )
        }
        assert "eval_probe" not in restored
        assert {"terminal", "read_file", "write_file"} <= restored
        print("PASS: mock count", len(mocks), "| restored tools", len(original))
    from agent.runtime_cwd import resolve_agent_cwd
    from tools.file_tools import read_file_tool, write_file_tool
    from tools.terminal_tool import _resolve_container_task_id, get_session_cwd, terminal_tool

    original_cwd = resolve_agent_cwd()
    # Relative paths must follow each scenario, even after another scenario has
    # warmed the actual Hermes file/terminal caches. Exercise exception cleanup too.
    with tempfile.TemporaryDirectory(prefix="cwd_probe_", dir=temporary_root) as temporary:
        for number in range(3):
            workspace = Path(temporary).resolve() / str(number)
            workspace.mkdir()
            task = "eval_workspace_probe_" + workspace.parent.name + "_" + str(number)
            try:
                with utils.hermes_workspace(workspace, task):
                    assert resolve_agent_cwd() == workspace
                    assert _resolve_container_task_id(task) == task
                    value = "scenario-" + str(number)
                    written = json.loads(write_file_tool("relative.txt", value, task_id=task))
                    assert (workspace / "relative.txt").read_text() == value, written
                    assert value in read_file_tool("relative.txt", task_id=task)
                    result = json.loads(terminal_tool("pwd", task_id=task, timeout=15))
                    expected = workspace.as_posix()
                    if os.name == "nt":
                        expected = "/" + expected[0].lower() + expected[2:]
                    assert (
                        result["exit_code"] == 0
                        and result["output"].strip().lower() == expected.lower()
                    ), result
                    if number == 1:
                        raise RuntimeError("intentional cleanup probe")
            except RuntimeError as exc:
                if str(exc) != "intentional cleanup probe":
                    raise
            assert get_session_cwd(task) is None
            assert resolve_agent_cwd() == original_cwd
            print("PASS: isolated relative file/terminal paths and cleanup", number)
    return 0


if __name__ == "__main__":
    sys.exit(main())
