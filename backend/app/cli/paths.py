"""Where DevPilot keeps a user's own files. Nothing here is a machine-specific constant: the home folder is the user's own."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

HOME_ENV = "DEVPILOT_HOME"


def default_home() -> Path:
    """DEVPILOT_HOME if set; else %LOCALAPPDATA%\\DevPilot on Windows, ~/.local/share/devpilot elsewhere."""
    configured = os.environ.get(HOME_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    return (Path(local) / "DevPilot") if local else (Path.home() / ".local" / "share" / "devpilot")


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def config(self) -> Path:
        return self.home / "config.toml"

    @property
    def credentials(self) -> Path:
        return self.home / "credentials.bin"  # encrypted for this Windows user (DPAPI); never plain text

    @property
    def approved_runtime(self) -> Path:
        return self.home / "approved-runtime.json"

    @property
    def approved_web_runtime(self) -> Path:
        return self.home / "approved-web-runtime.json"  # the website verifier's image, recorded only after its self-test passed

    @property
    def github_credentials(self) -> Path:
        return self.home / "github-token.bin"  # encrypted for this Windows user (DPAPI); never plain text

    @property
    def tools(self) -> Path:
        return self.home / "tools"

    @property
    def opencode_cache(self) -> Path:
        return self.tools / "opencode"

    @property
    def ripgrep_cache(self) -> Path:
        return self.tools / "ripgrep"

    @property
    def database(self) -> Path:
        return self.home / "data" / "devpilot.db"

    @property
    def workspaces(self) -> Path:
        return self.home / "workspaces"

    @property
    def checkpoints(self) -> Path:
        return self.home / "checkpoints"

    @property
    def work(self) -> Path:
        return self.home / "work"  # a run's disposable copy: outside the repository, as the launcher requires

    @property
    def guard(self) -> Path:
        return self.home / "guard"

    @property
    def ledger(self) -> Path:
        return self.home / "build-ledger.jsonl"

    def ensure(self) -> None:
        for folder in (self.home, self.tools, self.database.parent, self.workspaces, self.checkpoints, self.work, self.guard):
            folder.mkdir(parents=True, exist_ok=True)


def paths(home: Path | None = None) -> Paths:
    return Paths((home or default_home()).resolve())
