"""Private behavioral checks on disposable copies of native task artifacts."""

import hashlib
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

from .agent_container import docker_command
from .runtime_support import sha256_file, write_json_atomic

# This loader runs only in Docker. The authored check never executes on the host.
LOADER = r"""
import base64, json, os, pathlib, sys
p = json.loads(pathlib.Path('/tmp/artifact_input.json').read_text())
root = pathlib.Path(p['workspace'])
root.mkdir(parents=True, exist_ok=True)
for name, item in p['snapshot'].items():
    target = root / name
    if item['kind'] == 'directory':
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = item['text'].encode('utf-8') if 'text' in item else base64.b64decode(item['data_base64'], validate=True)
        target.write_bytes(raw)
os.chdir(root)
sys.path.insert(0, str(root))
result = pathlib.Path('/tmp/artifact_result.json')
result.write_text(json.dumps({'started': True}))
exec(compile(p['python'], '<private artifact check>', 'exec'),
     {'__name__': '__main__', 'WORKSPACE': str(root), 'VISIBLE_WORKSPACE': p['visible_workspace']})
result.write_text(json.dumps({'started': True, 'completed': True}))
"""


def evaluate_artifact_check(check, snapshot, *, image, workspace, visible_workspace, output):
    """Return a scored check; Docker/setup/capture failures raise as harness errors.

    Only artifact contents are cloned, not a running agent, memory, services or
    file permissions. This is behavioral testing, not proof against hostile code
    that deliberately attacks its verifier inside the same disposable container.
    """
    root = PurePosixPath(workspace)
    if (
        not root.is_absolute()
        or ".." in root.parts
        or not (root == PurePosixPath("/workspace") or root.is_relative_to("/home/worker"))
    ):
        raise ValueError("Unsupported native artifact workspace")
    for name, item in snapshot.items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            raise ValueError("Artifact snapshot path escapes its workspace")
        if item.get("kind") not in {"file", "directory"}:
            raise ValueError("Artifact verification requires regular files/directories")
    output = Path(output)
    output.mkdir(parents=True)
    payload = {
        "workspace": str(root),
        "visible_workspace": visible_workspace,
        "snapshot": snapshot,
        "python": check["python"],
    }
    payload_path = output / "input.json"
    write_json_atomic(payload_path, payload)
    name = "artifact-check-" + uuid.uuid4().hex[:12]
    flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    started = time.monotonic()
    created = False
    timed_out = False
    try:
        subprocess.run(
            docker_command(image, name, command=["python", "-c", LOADER]),
            check=True,
            capture_output=True,
            timeout=60,
            **flags,
        )
        created = True
        subprocess.run(
            ["docker", "cp", str(payload_path), name + ":/tmp/artifact_input.json"],
            check=True,
            capture_output=True,
            timeout=60,
            **flags,
        )
        with (
            (output / "stdout.log").open("wb") as stdout,
            (output / "stderr.log").open("wb") as stderr,
        ):
            try:
                subprocess.run(
                    ["docker", "start", "-a", name],
                    stdout=stdout,
                    stderr=stderr,
                    timeout=check.get("timeout_seconds", 30),
                    **flags,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                subprocess.run(
                    ["docker", "kill", name], check=True, capture_output=True, timeout=30, **flags
                )
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", name],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            **flags,
        )
        state = json.loads(inspected.stdout)
        if state["Running"] or state.get("Error"):
            raise RuntimeError(f"Artifact container did not finish normally: {state}")
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "result.json"
            copied = subprocess.run(
                ["docker", "cp", name + ":/tmp/artifact_result.json", str(marker)],
                capture_output=True,
                timeout=30,
                **flags,
            )
            completion = json.loads(marker.read_text()) if copied.returncode == 0 else {}
        if not completion.get("started") and not timed_out:
            raise RuntimeError(
                "Artifact verifier failed before entering the authored check; inspect stderr.log"
            )
        result = {
            "name": check["name"],
            "passed": not timed_out
            and state["ExitCode"] == 0
            and completion.get("completed") is True,
            "exit_code": state["ExitCode"],
            "timed_out": timed_out,
            "completed": completion.get("completed", False),
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "image": image,
            "check_sha256": hashlib.sha256(check["python"].encode()).hexdigest(),
            "input": str(payload_path),
            "stdout": str(output / "stdout.log"),
            "stderr": str(output / "stderr.log"),
            "scope": "Private check on an isolated artifact copy; no model call, tool event or agent-state mutation. File permissions, native memory and live services are not cloned.",
        }
    finally:
        if created:
            subprocess.run(
                ["docker", "rm", "-f", name], check=True, capture_output=True, timeout=30, **flags
            )
    result["elapsed_seconds"] = round(time.monotonic() - started, 6)
    result["evidence_sha256"] = {
        key: sha256_file(result[key]) for key in ("input", "stdout", "stderr")
    }
    write_json_atomic(output / "result.json", result)
    return result
