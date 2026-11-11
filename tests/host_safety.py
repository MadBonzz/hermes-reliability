"""Hermes host seams for plugin tests that run without an installed host."""

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


def host_safety(home, hermes_home, blocked=()):
    constants = ModuleType("hermes_constants")
    constants.get_real_home = lambda: Path(home)
    constants.get_hermes_home = lambda: Path(hermes_home)
    constants.get_default_hermes_root = lambda: Path(hermes_home)
    guard = ModuleType("agent.file_safety")
    guard.get_read_block_error = lambda path: (
        "Blocked by host policy" if str(path) in {str(p) for p in blocked} else None
    )
    return patch.dict(sys.modules, {"hermes_constants": constants, "agent.file_safety": guard})


class HostSafetyMixin:
    def setUp(self):
        super().setUp()
        self.enterContext(host_safety(self.root / "home", self.root / "hermes-profile"))
