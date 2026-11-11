"""Evaluator file-tool path guard; deliberately NOT an OS sandbox.

Terminal and arbitrary code remain available. They can bypass this boundary.
Record denials as evaluator interventions, never as native Hermes enforcement.
"""

import copy
import json
import os
import re
import shutil
import sys
import time
import venv
from contextlib import contextmanager
from pathlib import Path

FILE_TOOLS = {"read_file", "write_file", "patch", "search_files"}


@contextmanager
def isolated_python(runtime_root):
    """Route default Python/package commands to a disposable per-phase environment.

    Explicit host paths and other shell commands retain their native capabilities.
    Hermes strips VIRTUAL_ENV in terminal children, so uv also receives UV_PYTHON.
    """
    started = time.monotonic()
    environment = Path(runtime_root) / "python_env"
    venv.EnvBuilder(with_pip=True, system_site_packages=False).create(environment)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    if os.name == "nt":
        shutil.copyfile(python, scripts / "python3.exe")
    changes = {
        "PATH": str(scripts) + os.pathsep + os.environ.get("PATH", ""),
        "VIRTUAL_ENV": str(environment),
        "UV_PYTHON": str(python),
        "UV_PROJECT_ENVIRONMENT": str(environment),
        "UV_CACHE_DIR": str(Path(runtime_root) / "package_cache/uv"),
        "UV_SYSTEM_PYTHON": None,
        "PYTHONPATH": None,
        "PYTHONHOME": None,
        "PYTHONNOUSERSITE": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_REQUIRE_VIRTUALENV": "true",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_CACHE_DIR": str(Path(runtime_root) / "package_cache/pip"),
        "PIP_TARGET": None,
        "PIP_PREFIX": None,
        "PIP_USER": None,
    }
    previous = {key: os.environ.get(key) for key in changes}
    try:
        for key, value in changes.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield {
            "python": str(python),
            "base_python": sys.executable,
            "python_version": sys.version,
            "system_site_packages": False,
            "setup_seconds": round(time.monotonic() - started, 3),
            "scope": "default Python and pip/uv routing; explicit host paths remain accessible",
        }
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def checked_path(workspace, value, cwd=None):
    root = Path(workspace).resolve()
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("Expected a nonempty file path")
    target = Path(os.path.expandvars(value)).expanduser()
    target = (Path(cwd or root) / target).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Path is outside the scenario workspace")
    if target.is_file() and target.stat().st_nlink > 1:
        raise ValueError("Hard-linked files are outside the supported path boundary")
    return target


def guarded_arguments(name, arguments, workspace, cwd=None):
    args = copy.deepcopy(arguments)
    if not isinstance(args, dict):
        raise ValueError("Expected object arguments")
    if name == "patch" and args.get("mode") == "patch":
        body = args.get("patch", "")
        count = 0

        def single(match):
            nonlocal count
            count += 1
            return match[1] + str(checked_path(workspace, match[2].strip(), cwd))

        def move(match):
            nonlocal count
            count += 1
            source = checked_path(workspace, match[2].strip(), cwd)
            destination = checked_path(workspace, match[3].strip(), cwd)
            return f"{match[1]}{source} -> {destination}"

        body = re.sub(
            r"^(\*\*\*\s*(?:Add|Update|Delete)\s+File:\s*)(.+)$", single, body, flags=re.M
        )
        body = re.sub(r"^(\*\*\*\s*Move\s+File:\s*)(.+?)\s*->\s*(.+)$", move, body, flags=re.M)
        body = re.sub(r"^(\*\*\*\s*Move\s+to:\s*)(.+)$", single, body, flags=re.M)
        if not count:
            raise ValueError("Patch has no supported file headers")
        args["patch"] = body
    else:
        default = "." if name == "search_files" else ""
        target = checked_path(workspace, args.get("path", default), cwd)
        args["path"] = str(target)
        if name == "search_files" and target.is_dir():
            for entry in target.rglob("*"):
                checked_path(workspace, str(entry))
    return args


@contextmanager
def file_tool_guard(registry, workspace, cwd_resolver=None, *, neutral_messages=False):
    originals = {}
    events = []
    try:
        for name in FILE_TOOLS:
            entry = registry._tools.get(name)
            if entry is None or not hasattr(entry, "handler"):
                raise RuntimeError(f"Cannot guard installed Hermes file tool: {name}")
            originals[name] = entry
            replacement = copy.copy(entry)

            def handler(arguments, _name=name, _handler=entry.handler, **kwargs):
                try:
                    cwd = cwd_resolver(kwargs.get("task_id")) if cwd_resolver else None
                    safe = guarded_arguments(_name, arguments, workspace, cwd)
                except (ValueError, OSError, RuntimeError) as exc:
                    events.append(
                        {
                            "tool": _name,
                            "arguments": arguments,
                            "reason": str(exc),
                            "enforced_by": "evaluation_harness",
                        }
                    )
                    return json.dumps(
                        {
                            "error": str(exc).replace("scenario workspace", "working directory")
                            if neutral_messages
                            else str(exc),
                            "code": "PATH_DENIED" if neutral_messages else "EVAL_PATH_DENIED",
                            "allowed_root": str(workspace),
                        }
                    )
                return _handler(safe, **kwargs)

            replacement.handler = handler
            registry._tools[name] = replacement
        yield {
            "mode": "file_tool_paths",
            "workspace": str(workspace),
            "os_sandbox": False,
            "terminal_and_code_restricted": False,
            "denials": events,
        }
    finally:
        registry._tools.update(originals)
