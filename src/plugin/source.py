"""Prepare an owned source snapshot without changing the operator's checkout."""

import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from ..orchestrator.source import is_git, snapshot_directory
from .source_policy import host_read_allowed, local_source

REPOSITORIES = {
    "hermes": "https://github.com/NousResearch/hermes-agent.git",
    "openhands": "https://github.com/OpenHands/software-agent-sdk.git",
    "deerflow": "https://github.com/bytedance/deer-flow.git",
}


def git(*args):
    return subprocess.check_output(
        ["git", *args], text=True, encoding="utf-8", stderr=subprocess.PIPE, timeout=600
    ).strip()


def resolve_source(config, folder):
    """Clone into owned run data, pin a commit, and leave the user's checkout alone."""
    location = config.pop("source_input")
    requested = config.pop("requested_revision", "")
    destination = folder / "agent_source"
    local = urlsplit(location).scheme != "https"
    if local:
        # Recheck in the owning worker profile, before Git or snapshot reads.
        location = str(local_source(location))
    if local and (
        not is_git(location) or (config["adapter"]["kind"] == "custom" and not requested)
    ):
        if requested or config.get("requested_base"):
            raise ValueError("Revision selection requires a Git repository")
        if config.get("execute") and config["adapter"]["kind"] != "custom":
            raise ValueError("Built-in execution requires a pinned Git checkout")
        config["source"] = snapshot_directory(location, destination, read_guard=host_read_allowed)
        previous = config.pop("previous_source", None)
        if previous and previous["repository"] != config["source"]["repository"]:
            raise ValueError("Saved tests belong to a different source directory")
        return config
    if local:
        location = str(Path(location).resolve())
        if git("-C", location, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Commit or stash tracked changes before comparing a local checkout")
        revision = requested or git("-C", location, "rev-parse", "HEAD")
        try:
            repository = git("-C", location, "remote", "get-url", "origin")
        except subprocess.CalledProcessError:
            repository = Path(location).as_uri()
    else:
        repository, revision = location, requested
    if config["adapter"]["kind"] in {"openhands", "deerflow"} and config.get("execute", True):
        image = json.loads(
            subprocess.check_output(
                ["docker", "image", "inspect", config["adapter"]["runtime_image"]], text=True
            )
        )[0]
        labels = image["Config"].get("Labels") or {}
        if labels.get("org.agent-evaluation.runtime") != config["adapter"]["kind"]:
            raise ValueError("Docker image runtime does not match the selected agent")
        image_revision = labels.get("org.opencontainers.image.revision")
        if not image_revision:
            raise ValueError("Docker image has no source revision label")
        revision = revision or image_revision
        config["adapter"]["runtime_image"] = image["Id"]
    git("clone", "--no-hardlinks", location, str(destination))
    if revision:
        if revision.startswith("-"):
            raise ValueError("Invalid revision")
        git("-C", str(destination), "checkout", "--detach", revision)
    revision = git("-C", str(destination), "rev-parse", "HEAD")
    git("-C", str(destination), "remote", "set-url", "origin", repository)
    config["source"] = {
        "repository": repository,
        "revision": revision,
        "local_path": str(destination),
    }
    previous = config.pop("previous_source", None)
    if previous and previous["repository"].removesuffix(".git") != repository.removesuffix(".git"):
        raise ValueError("Saved tests belong to a different repository")
    base = config.pop("requested_base", "")
    if not base and previous and config["adapter"]["kind"] == "hermes":
        if previous["revision"] != revision:
            base = previous["revision"]
    if base:
        base_revision = git("-C", str(destination), "rev-parse", "--verify", base + "^{commit}")
        if base_revision == revision:
            raise ValueError("Base and target revisions are identical")
        base_path = folder / "agent_source_base"
        git("clone", "--no-hardlinks", str(destination), str(base_path))
        git("-C", str(base_path), "checkout", "--detach", base_revision)
        git("-C", str(base_path), "remote", "set-url", "origin", repository)
        config["source"].update(base_revision=base_revision, base_local_path=str(base_path))
    return config
