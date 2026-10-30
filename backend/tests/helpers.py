"""Shared test constants and builders. Every credential here is fake."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
        "coding_checkpoints_dir": tmp_path / "checkpoints",
        "egress_allowlist_file": ALLOWLIST_FILE,
        "openai_api_key": FAKE_OPENAI_KEY,
        "github_token": FAKE_GITHUB_TOKEN,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _rule(permission: str, pattern: str, action: str) -> dict[str, str]:
    return {"permission": permission, "pattern": pattern, "action": action}


# OpenCode 1.19.5's own rules before and after the configured ones, exactly as GET /agent listed them
# for the build agent in the Phase 4C baseline run (rules 0-13 and 100 of 101).
OPENCODE_DEFAULT_RULES = [
    _rule("*", "*", "allow"),
    _rule("doom_loop", "*", "ask"),
    _rule("external_directory", "*", "ask"),
    _rule("external_directory", "/tmp/home/.local/share/opencode/tool-output/*", "allow"),
    _rule("external_directory", "/tmp/opencode/*", "allow"),
    _rule("question", "*", "deny"),
    _rule("plan_enter", "*", "deny"),
    _rule("plan_exit", "*", "deny"),
    _rule("read", "*", "allow"),
    _rule("read", "*.env", "ask"),
    _rule("read", "*.env.*", "ask"),
    _rule("read", "*.env.example", "allow"),
    _rule("question", "*", "allow"),
    _rule("plan_enter", "*", "allow"),
]
OPENCODE_TAIL_RULES = [_rule("external_directory", "/tmp/home/.local/share/opencode/tool-output/*", "allow")]
OPENCODE_1_19_5_TOOLS = [
    "invalid", "question", "bash", "read", "glob", "grep", "edit", "write",
    "task", "webfetch", "todowrite", "websearch", "skill", "apply_patch",
]  # fmt: skip


def opencode_rule_list(permission: dict[str, Any]) -> list[dict[str, str]]:
    """A configuration's permission object in the list form OpenCode enforces, keeping its key order."""
    rules: list[dict[str, str]] = []
    for name, value in permission.items():
        if isinstance(value, str):
            rules.append(_rule(name, "*", value))
        else:
            rules += [_rule(name, pattern, action) for pattern, action in value.items()]
    return rules


def opencode_agents(config: dict[str, Any], *, extra_rules: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """GET /agent as OpenCode 1.19.5 reports it for this merged configuration: its defaults, the global
    rules, the build agent's rules, any extra (later) rules, then its tail; plus the hidden system agents."""
    build = [
        *OPENCODE_DEFAULT_RULES,
        *opencode_rule_list(config["permission"]),
        *opencode_rule_list(config["agent"]["build"]["permission"]),
        *(extra_rules or []),
        *OPENCODE_TAIL_RULES,
    ]
    system = [
        {"name": name, "mode": "primary", "native": True, "hidden": True, "permission": opencode_rule_list(config["permission"])}
        for name in ("compaction", "summary", "title")
    ]
    return [{"name": "build", "mode": "primary", "native": True, "hidden": None, "permission": build}, *system]
