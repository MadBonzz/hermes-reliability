"""Freeze this plugin's controller separately from generated evaluator workspaces."""

import hashlib
import importlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

from ..evaluation.runtime_support import sha256_file, write_json_atomic

ROOT = Path(__file__).resolve().parents[2]


def freeze(folder):
    destination = folder / "controller"
    destination.mkdir()
    shutil.copytree(
        ROOT / "src", destination / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    for name in ("__init__.py", "scenario.schema.json"):
        shutil.copy2(ROOT / name, destination / name)
    hashes = {
        str(path.relative_to(destination)): sha256_file(path)
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    }
    write_json_atomic(folder / "controller_manifest.json", hashes)


def verify(folder):
    root = folder / "controller"
    hashes = json.loads((folder / "controller_manifest.json").read_text())
    for name, expected in hashes.items():
        path = root / name
        if (
            not path.resolve().is_relative_to(root.resolve())
            or path.is_symlink()
            or sha256_file(path) != expected
        ):
            raise ValueError("Frozen controller changed: " + name)
    return root


def service_class(folder):
    root = verify(folder)
    name = "_reliability_controller_" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, root / "__init__.py", submodule_search_locations=[str(root)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(".src.plugin.service", name).Service
