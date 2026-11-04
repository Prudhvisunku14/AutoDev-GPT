"""The person's own GitHub token, for reading private repositories. Optional: public repositories need none.

Sources, in order: the GITHUB_TOKEN environment variable, then GH_TOKEN, then a file encrypted with Windows DPAPI for this
Windows user (the same protection as the OpenAI key). It is never printed, logged, put in a URL or sent anywhere but
api.github.com. A fine-grained token with read-only "Contents" access to the one repository is enough.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.cli.credentials import CredentialError, _transform, store_available

ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")
_PLAUSIBLE = re.compile(r"^(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{40,255})$")


def plausible(token: str) -> bool:
    """A format check only: it says nothing about what the token may read."""
    return bool(_PLAUSIBLE.fullmatch(token))


def store(path: Path, token: str) -> None:
    if not store_available():
        raise CredentialError(f"There is no encrypted store on this system: set the {ENV_VARS[0]} environment variable instead")
    if not plausible(token):
        raise CredentialError("That does not look like a GitHub token (it starts with github_pat_ or ghp_)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_transform(token.encode("utf-8"), protect=True))


def stored(path: Path) -> str | None:
    if not store_available() or not path.is_file():
        return None
    try:
        return _transform(path.read_bytes(), protect=False).decode("utf-8")
    except (OSError, CredentialError, UnicodeDecodeError):
        return None


def load(path: Path) -> str | None:
    for name in ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return stored(path)


def source(path: Path) -> str | None:
    """Where a token would come from, without reading its value into any output."""
    for name in ENV_VARS:
        if os.environ.get(name, "").strip():
            return f"the {name} environment variable"
    return "the encrypted store" if stored(path) else None


def forget(path: Path) -> bool:
    if path.is_file():
        path.unlink()
        return True
    return False
