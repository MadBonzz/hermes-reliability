"""Source discovery for Git repositories and private snapshots of local code."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from ..evaluation.runtime_support import sha256_file

EXCLUDED = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".cache",
    "dist",
    "build",
    "eval_harness",
    ".aws",
    ".ssh",
    ".codex",
    ".agents",
    ".config",
    ".gnupg",
    ".docker",
    ".kube",
    ".azure",
    ".mozilla",
    "browser-profile",
    "browser-profiles",
    "user data",
    "google-chrome",
    "chrome",
    "chromium",
    "firefox",
    "brave-browser",
    "microsoft edge",
    "edge",
    "opera",
    "vivaldi",
    "tor browser",
    "safari",
    "auth.json",
    "auth.lock",
    "config.yaml",
    "config.yml",
    ".netrc",
    "_netrc",
    ".git-credentials",
    ".pgpass",
    ".npmrc",
    ".pypirc",
}


def permitted(path):
    return not any(part.lower() in EXCLUDED for part in path.parts) and not (
        path.name.lower().startswith(".env") or path.suffix.lower() in {".pem", ".key", ".p12"}
    )


def is_git(root):
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and Path(result.stdout.strip()).resolve() == Path(root).resolve()


def source_files(root, *, tracked=True, read_guard=None):
    """Tracked Git files, or regular local files; never follow linked directories."""
    root = Path(root).resolve()
    if read_guard and not read_guard(root):
        raise ValueError("Source directory is blocked by the host read guard")
    if tracked and is_git(root):
        names = (
            subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"], text=True)
            .rstrip("\0")
            .split("\0")
        )
    else:
        names = []
        pending = [root]
        while pending:
            for path in sorted(pending.pop().iterdir()):
                relative = path.relative_to(root)
                if not permitted(relative) or path.is_symlink() or (read_guard and not read_guard(path)):
                    continue
                if path.is_dir():
                    pending.append(path)
                elif path.is_file():
                    names.append(relative.as_posix())
    return sorted(
        name
        for name in names
        if name
        and permitted(Path(name))
        and (root / name).is_file()
        and not (root / name).is_symlink()
        and (root / name).resolve().is_relative_to(root)
        and permitted((root / name).resolve().relative_to(root))
        and (not read_guard or read_guard(root / name))
    )


def read_source(root, name):
    relative = Path(name)
    path = (Path(root) / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()) or not permitted(relative):
        raise ValueError("Source read outside allowed repository files")
    if name not in source_files(root):
        raise ValueError("Source file is not in the inspected source inventory")
    if path.stat().st_size > 2_000_000:
        raise ValueError("Source file exceeds the 2 MB inspection limit")
    if "\x00" in path.read_text(encoding="utf-8-sig"):
        raise ValueError("Source reads require text/code, not binary content")
    return path


def snapshot_directory(location, destination, *, read_guard=None):
    """Copy code without mutating the input; record exact content provenance."""
    root, destination = Path(location).resolve(), Path(destination).resolve()
    if destination.is_relative_to(root):
        raise ValueError("Source snapshot destination must be outside the input directory")
    files = source_files(root, tracked=False, read_guard=read_guard)
    if not files:
        raise ValueError("Local source directory contains no eligible files")
    if len(files) > 20000 or sum((root / name).stat().st_size for name in files) > 100_000_000:
        raise ValueError("Local source snapshot exceeds 20,000 files or 100 MB")
    destination.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name in files:
        source = root / name
        if read_guard and not read_guard(source):
            raise ValueError("Source read policy changed during snapshot")
        before = sha256_file(source)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if sha256_file(target) != before or sha256_file(source) != before:
            raise ValueError(f"Source changed during snapshot: {name}")
        hashes[name] = before
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return {
        "repository": root.as_uri(),
        "revision": "sha256:" + digest,
        "local_path": str(destination),
        "kind": "directory",
        "sha256": hashes,
    }


def verify_snapshot(config):
    root = Path(config["local_path"]).resolve()
    actual = {name: sha256_file(root / name) for name in source_files(root)}
    digest = hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest()
    if actual != config["sha256"] or config["revision"] != "sha256:" + digest:
        raise ValueError("Local source snapshot changed")
    return root
