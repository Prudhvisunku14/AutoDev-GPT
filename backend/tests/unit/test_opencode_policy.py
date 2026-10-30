"""OpenCode's permission policy: the managed config, the effective-config check and protected paths."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.coding.opencode_policy import (
    evaluate,
    is_protected,
    managed_config,
    session_rules,
    verify_effective_config,
)

GATEWAY = "http://devpilot-model-gateway:8787/v1"


def _config() -> dict[str, Any]:
    return managed_config(model="openai/test-model", gateway_base_url=GATEWAY)


# --- Rule evaluation ---------------------------------------------------------------


def test_the_last_matching_rule_wins() -> None:
    rules = [("*", "*", "deny"), ("edit", "*", "allow"), ("edit", "*.env", "deny")]
    assert evaluate(rules, "edit", "main.py") == "allow"
    assert evaluate(rules, "edit", "config/.env") == "deny"
    assert evaluate(rules, "bash", "ls") == "deny"


def test_an_unmatched_permission_is_allowed_like_opencodes_default() -> None:
    assert evaluate([("edit", "*", "deny")], "bash", "ls") == "allow"


@pytest.mark.parametrize(
    ("pattern", "value", "matches"),
    [
        ("*", "", True),
        ("*", "a/b/c", True),  # `*` crosses path separators
        ("git *", "git status", True),
        ("git *", "gitx", False),
        ("?.py", "a.py", True),
        ("?.py", "ab.py", False),
        ("a.b", "aXb", False),  # every other character is literal, including regex metacharacters
        ("[x]", "[x]", True),
    ],
)
def test_wildcards_follow_opencodes_syntax(pattern: str, value: str, matches: bool) -> None:
    assert (evaluate([("p", pattern, "deny")], "p", value) == "deny") is matches


# --- The managed configuration ------------------------------------------------------


def test_the_managed_config_passes_its_own_check() -> None:
    assert verify_effective_config(_config()) == []


def test_the_managed_config_holds_no_credential_and_points_at_the_gateway() -> None:
    config = _config()
    options = config["provider"]["openai"]["options"]
    assert options["baseURL"] == GATEWAY
    assert options["apiKey"] == "devpilot-model-gateway"  # a placeholder; the gateway adds the real key
    assert config["enabled_providers"] == ["openai"]


def test_the_managed_config_denies_unknown_permissions_by_default() -> None:
    permission = _config()["permission"]
    assert next(iter(permission)) == "*" and permission["*"] == "deny"


def test_session_rules_match_the_managed_policy() -> None:
    rules = [(r["permission"], r["pattern"], r["action"]) for r in session_rules()]
    assert evaluate(rules, "bash", "python main.py") == "deny"
    assert evaluate(rules, "edit", "calculator.py") == "allow"
    assert evaluate(rules, "edit", ".opencode/plugin/x.js") == "deny"
    assert evaluate(rules, "read", ".env") == "deny"
    assert evaluate(rules, "something_new", "x") == "deny"


# --- Unsafe effective configurations -------------------------------------------------


def _with(change: dict[str, Any]) -> dict[str, Any]:
    config = _config()
    config.update(copy.deepcopy(change))
    return config


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"lsp": True}, "lsp must be false"),
        ({"formatter": {}}, "formatter must be false"),
        ({"plugin": ["opencode-evil-plugin"]}, "plugins must be empty"),
        ({"mcp": {"tool": {"type": "local", "command": ["x"]}}}, "MCP servers"),
        ({"mcp": {"tool": {"type": "local", "enabled": True}}}, "MCP servers"),
        ({"share": "manual"}, "share must be"),
        ({"autoupdate": True}, "autoupdate must be false"),
        ({"enabled_providers": ["openai", "anthropic"]}, "enabled_providers"),
        ({"enabled_providers": []}, "enabled_providers"),
    ],
)
def test_unsafe_engine_settings_are_reported(change: dict[str, Any], expected: str) -> None:
    problems = verify_effective_config(_with(change))
    assert any(expected in p for p in problems), problems


def test_disabled_mcp_servers_are_accepted() -> None:
    assert verify_effective_config(_with({"mcp": {"tool": {"type": "local", "enabled": False}}})) == []


def test_a_single_bash_allow_pattern_is_caught() -> None:
    config = _config()
    config["permission"]["bash"] = {"*": "deny", "python *": "allow"}
    problems = verify_effective_config(config)
    assert any("bash is not denied for 'python main.py'" in p for p in problems), problems


def test_a_rule_added_later_that_re_allows_a_protected_file_is_caught() -> None:
    config = _config()
    config["permission"]["edit"]["*.json"] = "allow"  # added after the opencode.json deny, so it wins
    problems = verify_effective_config(config)
    assert any("edit is not denied" in p and "opencode.json" in p for p in problems), problems


def test_dropping_the_catch_all_deny_is_caught() -> None:
    config = _config()
    del config["permission"]["*"]
    problems = verify_effective_config(config)
    assert any("devpilot_future_permission is not denied" in p for p in problems), problems


def test_an_agent_that_re_allows_bash_is_caught() -> None:
    config = _config()
    config["agent"]["build"] = {"permission": {"bash": "allow"}}
    problems = verify_effective_config(config)
    assert any(p.startswith("agent build: bash is not denied") for p in problems), problems


def test_a_re_enabled_subagent_with_web_access_is_caught() -> None:
    config = _config()
    config["agent"]["general"] = {"disable": False, "permission": {"webfetch": "allow"}}
    assert any(p.startswith("agent general: webfetch") for p in verify_effective_config(config))


def test_rules_in_the_list_form_are_checked_too() -> None:
    config = _config()
    config["permission"] = session_rules()
    assert verify_effective_config(config) == []
    config["permission"] = [*session_rules(), {"permission": "bash", "pattern": "*", "action": "allow"}]
    assert any("bash is not denied" in p for p in verify_effective_config(config))


def test_a_global_allow_everything_string_is_caught() -> None:
    assert any("bash is not denied" in p for p in verify_effective_config(_with({"permission": "allow"})))


@pytest.mark.parametrize(
    "permission",
    [42, ["not a rule"], [{"permission": "bash"}], {"bash": 1}, "maybe"],
)
def test_permission_rules_in_an_unknown_format_are_refused(permission: Any) -> None:
    problems = verify_effective_config(_with({"permission": permission}))
    assert any("unknown format" in p for p in problems), problems


def test_missing_permission_rules_mean_opencodes_allow_defaults_and_are_refused() -> None:
    config = _config()
    del config["permission"]
    problems = verify_effective_config(config)
    assert any("bash is not denied" in p for p in problems)


def test_a_policy_that_blocks_project_edits_is_reported() -> None:
    config = _config()
    config["permission"]["edit"] = "deny"
    assert any("edit must be allowed" in p for p in verify_effective_config(config))


def test_a_config_that_is_not_an_object_is_refused() -> None:
    assert verify_effective_config(["not", "an", "object"]) == ["the engine's configuration is not a JSON object"]


# --- Protected paths ------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "opencode.json", "OpenCode.JSON", "sub/opencode.jsonc", ".opencode/plugin/x.js", "a/.opencode/agent/b.md",
        "AGENTS.md", "docs/claude.md", ".git/config", "pkg/.git/HEAD", ".env", "app/.env", ".env.local",
        ".devpilot/junit.xml", "sub\\.opencode\\x.js",
    ],
)  # fmt: skip
def test_protected_paths(path: str) -> None:
    assert is_protected(path)


@pytest.mark.parametrize(
    "path",
    ["calculator.py", "test_calculator.py", "src/app/main.py", ".env.example", "docs/opencode-notes.md", "env.py", "sub/.devpilot/x"],
)
def test_ordinary_project_files_are_not_protected(path: str) -> None:
    assert not is_protected(path)
