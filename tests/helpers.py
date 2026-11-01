import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import utils
from src.mock_registry import DynamicMockToolManager


class FakeRegistry:
    def __init__(self):
        self._tools = {
            name: SimpleNamespace(handler=lambda args, **kwargs: '{"status":"ok"}')
            for name in ("terminal", "read_file", "write_file", "patch", "search_files")
        }
        self._generation = 0
        self._toolset_checks = {"terminal": object()}
        self._toolset_aliases = {"shell": "terminal"}

    def register(self, name, **kwargs):
        self._tools[name] = kwargs
        self._generation += 1


def mock(name="mock"):
    return {
        "name": name,
        "description": "test",
        "parameters": {"type": "object", "properties": {}},
        "behavior": {"type": "static_return"},
    }


def scenario(name="sample", mocks=None):
    return {
        "id": name,
        "name": name,
        "property_tested": "test",
        "mock_tools": mocks or [],
        "turns": [
            {
                "turn_number": 1,
                "prompt": "Create a file",
                "state_checks": [{"type": "file_exists", "path": "done.txt"}],
                "process_checks": [{"type": "tool_called", "tool_name": "write_file"}],
                "output_checks": [{"type": "contains_any", "values": ["done"]}],
            }
        ],
    }


class FakeAgent:
    def __init__(self, workspace):
        self.workspace = workspace
        self.tools = [{"type": "function", "function": {"name": n}} for n in utils.registry._tools]
        self.valid_tool_names = set(utils.registry._tools)
        self.closed = False

    def run_conversation(self, user_message, conversation_history, task_id):
        args = {"path": str(self.workspace / "done.txt"), "content": "ok"}
        self.tool_start_callback("call1", "write_file", args)
        (self.workspace / "done.txt").write_text("ok")
        self.tool_complete_callback("call1", "write_file", args, {"status": "ok"})
        return {
            "final_response": "done",
            "messages": [
                {"role": "user", "content": user_message},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call1",
                            "function": {"name": "write_file", "arguments": json.dumps(args)},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call1", "content": "ok"},
                {"role": "assistant", "content": "done"},
            ],
        }

    def close(self):
        self.closed = True


class TemporaryDirectoryTestCase(unittest.TestCase):
    """Give each test an isolated scratch directory with automatic cleanup."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)


class RegistryTestCase(TemporaryDirectoryTestCase):
    """Use a fake Hermes registry; no installed agent or model is required."""

    def setUp(self):
        super().setUp()
        self.registry = FakeRegistry()
        self.toolsets = SimpleNamespace(
            TOOLSETS={"terminal": {"tools": ["terminal"]}},
            CORE_TOOLS={"terminal"},
            _resolve_toolset_memo={"stale": True},
        )
        self.cached = {"old": True}
        self.patcher = patch.multiple(
            utils,
            tool_working_directory=lambda task_id: None,
            registry=self.registry,
            toolsets=self.toolsets,
            model_tools=SimpleNamespace(_clear_tool_defs_cache=self.cached.clear),
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def make_mock_manager(self, workspace, flags):
        return DynamicMockToolManager(
            workspace,
            flags,
            registry=self.registry,
            toolsets=self.toolsets,
            model_tools=utils.model_tools,
        )
