"""Explicit environment boundary for children that load target or authored code."""

import os

RUNTIME_ENV = frozenset({
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
})


def child_environment():
    # Never inherit provider credentials, connection strings, Python import hooks,
    # or host approval flags. Callers add only the credentials needed by that phase.
    return {**{name: os.environ[name] for name in RUNTIME_ENV if name in os.environ},
            "PYTHONNOUSERSITE": "1"}
