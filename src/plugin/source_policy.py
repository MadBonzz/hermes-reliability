"""Apply the active Hermes profile's read policy before inspecting local input."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from ..orchestrator.source import permitted


def host_read_allowed(path):
    # Use the host's shared guard, including its raw-path checks. Fail closed if
    # this required guard is unavailable; never substitute bare filesystem reads.
    from agent.file_safety import get_read_block_error

    return get_read_block_error(str(path)) is None


def local_source(path):
    from hermes_constants import get_default_hermes_root, get_hermes_home, get_real_home

    if not host_read_allowed(path):
        raise ValueError("Source path is blocked by Hermes' read guard")
    root = Path(path).expanduser().resolve()
    homes = {Path.home().resolve(), Path(get_real_home()).resolve()}
    hermes_homes = {Path(get_hermes_home()).resolve(), Path(get_default_hermes_root()).resolve()}
    if any(root == home or root in home.parents for home in homes):
        raise ValueError("Choose a code directory, not a home directory or its ancestor")
    if any(root.is_relative_to(home) or home.is_relative_to(root) for home in hermes_homes):
        raise ValueError("Hermes home and its contents cannot be used as source")
    if not permitted(root) or not host_read_allowed(root):
        raise ValueError("Credential and protected directories cannot be used as source")
    if not root.is_dir():
        raise ValueError("Provide an existing local directory or HTTPS Git repository")
    return root


def approval_source_key(args):
    """Scope persistent approval to a normalized source identity and revision."""
    source = str(args.get("source", "")).strip()
    if urlsplit(source).scheme != "https":
        source = str(local_source(source))
    identity = {
        "source": source,
        "revision": str(args.get("revision", "")).strip(),
        "runtime": args.get("runtime", "custom"),
        "mode": args.get("mode", "explore"),
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return "hermes-reliability.execute." + digest
