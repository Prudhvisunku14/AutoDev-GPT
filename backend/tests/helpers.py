"""Shared test constants and builders. Every credential here is fake."""

from __future__ import annotations

from pathlib import Path

from app.config import REPO_ROOT, Settings

API_TOKEN = "test-api-token-" + "a" * 40
FAKE_OPENAI_KEY = "sk-test" + "Z" * 40
FAKE_GITHUB_TOKEN = "ghp_" + "Q" * 36
ALLOWLIST_FILE = REPO_ROOT / "config" / "egress_allowlist.yaml"


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "devpilot_api_token": API_TOKEN,
        "database_url": f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}",
        "workspaces_dir": tmp_path / "workspaces",
        "egress_allowlist_file": ALLOWLIST_FILE,
        "openai_api_key": FAKE_OPENAI_KEY,
        "github_token": FAKE_GITHUB_TOKEN,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]
