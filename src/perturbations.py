"""Apply evaluator-controlled filesystem, fault-flag, and history changes."""

import copy
import inspect
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from .utils import resolve_ws_path


def _compress_history(history, agent, settings, event):
    """Try native compression, recording when deterministic truncation is used instead."""
    keep_n = int(settings.get("keep_last_n_messages", 0))
    use_native = bool(settings.get("use_native_compressor", False))

    # If requested, try Hermes's native ContextCompressor first
    compressed_ok = False
    if (
        use_native
        and agent is not None
        and hasattr(agent, "context_compressor")
        and agent.context_compressor
    ):
        comp = agent.context_compressor
        orig_first = getattr(comp, "protect_first_n", 3)
        orig_last = getattr(comp, "protect_last_n", 4)
        try:
            comp.protect_first_n = 0
            comp.protect_last_n = keep_n
            kwargs = (
                {"force": True} if "force" in inspect.signature(comp.compress).parameters else {}
            )
            before = copy.deepcopy(history)
            new_hist = comp.compress(copy.deepcopy(history), **kwargs)
            if isinstance(new_hist, list) and new_hist != before:
                history[:] = new_hist
                compressed_ok = True
            else:
                event["native_fallback_reason"] = "Native compressor returned unchanged history"
        except Exception as exc:
            event["native_fallback_reason"] = str(exc)
        finally:
            comp.protect_first_n = orig_first
            comp.protect_last_n = orig_last

    if not compressed_ok:
        if keep_n <= 0:
            history.clear()
            history.append(
                {
                    "role": "user",
                    "content": "[CONTEXT SUMMARY: Earlier conversation turns were compacted to save context window space. Consult persistent workspace files for saved rules or state.]",
                }
            )
        elif len(history) > keep_n:
            del history[:-keep_n]
            # Do not leave orphaned tool responses at the front of a truncated history.
            while history and history[0].get("role") == "tool":
                history.pop(0)
    event["applied_method"] = "native_compression" if compressed_ok else "history_truncation"


def apply_perturbations(
    perturbations: list[dict[str, Any]],
    workspace: Path,
    fault_flags: dict[str, bool],
    history: list[dict[str, Any]],
    agent: Any | None = None,
    events: list[dict[str, Any]] | None = None,
    mock_services=None,
) -> list[dict[str, Any]]:
    """Executes declarative pre-turn perturbations or post-turn cleanups."""
    for p in perturbations:
        p_type = p["type"]
        event = {"type": p_type, "requested": copy.deepcopy(p)}
        if events is not None:
            events.append(event)

        if p_type == "set_mock_state":
            if mock_services is None:
                raise ValueError("set_mock_state requires the scenario's service manager")
            mock_services.patch(p["service"], p["values"])

        elif p_type in ("write_file", "append_file"):
            target = resolve_ws_path(workspace, p["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            content = p.get("content", "").replace("{WORKSPACE}", workspace.as_posix())
            mode = "w" if p_type == "write_file" else "a"
            with target.open(mode, encoding="utf-8", newline="") as stream:
                stream.write(content)

        elif p_type == "delete_file":
            target = resolve_ws_path(workspace, p["path"])
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()

        elif p_type == "mkdir":
            target = resolve_ws_path(workspace, p["path"])
            target.mkdir(parents=True, exist_ok=True)

        elif p_type == "chmod":
            target = resolve_ws_path(workspace, p["path"])
            if not target.exists():
                raise FileNotFoundError(f"Cannot inject permission fault: {target}")
            mode_int = int(str(p["mode"]), 8)
            # Windows's read-only attribute is sufficient for files, but not directories.
            if os.name == "nt" and target.is_dir():
                # Remove the deny BEFORE chmod: the deny includes write attributes.
                # Avoid generic W, whose SYNCHRONIZE bit also blocks directory reads.
                subprocess.run(
                    ["icacls", str(target), "/remove:d", "*S-1-1-0"],
                    capture_output=True,
                    check=True,
                )
                os.chmod(target, mode_int)
                if (mode_int & 0o222) == 0:
                    subprocess.run(
                        ["icacls", str(target), "/deny", "*S-1-1-0:(WD,AD,WEA,WA)"],
                        capture_output=True,
                        check=True,
                    )
            else:
                os.chmod(target, mode_int)

        elif p_type == "set_fault_flag":
            fault_flags[p["flag_name"]] = bool(p.get("flag_value", True))

        elif p_type in ("compress_context", "truncate_history"):
            _compress_history(history, agent, p, event)
        else:
            raise ValueError(f"Unknown perturbation type: {p_type}")
        event["applied"] = True

    return history


def force_unlock_workspace(workspace: Path) -> None:
    """Ensures any chmod 444/555 files or directories are restored to 755/644 before rmtree."""
    if not workspace.exists():
        return
    if os.name == "nt":
        subprocess.run(
            ["icacls", str(workspace), "/remove:d", "*S-1-1-0", "/t", "/c", "/q"],
            capture_output=True,
            check=True,
        )
    for root, dirs, files in os.walk(workspace):
        for d in dirs:
            p = Path(root) / d
            try:
                os.chmod(p, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)
            except Exception:
                pass
        for f in files:
            p = Path(root) / f
            try:
                os.chmod(p, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
            except Exception:
                pass
