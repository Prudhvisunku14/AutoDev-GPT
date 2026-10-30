"""OpenCode's permission policy: the managed config, the effective-policy check and protected paths.

Rule order is judged from GET /agent (the ordered rules OpenCode enforces); GET /config is checked
only where order does not matter, because the real 1.19.5 server re-orders its permission keys.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.coding.opencode_policy import (
    agent_rule_problems,
    config_problems,
    enforced_rules,
    evaluate,
    hidden_from_model,
    is_protected,
    managed_config,
    ruleset_problems,
    session_rules,
    verify_effective_config,
)
from tests.helpers import OPENCODE_DEFAULT_RULES, opencode_agents, opencode_rule_list

GATEWAY = "http://devpilot-model-gateway:8787/v1"


def _config() -> dict[str, Any]:
    return managed_config(model="openai/test-model", gateway_base_url=GATEWAY)


def _reordered_like_the_server(permission: dict[str, Any]) -> dict[str, Any]:
    """The key order GET /config returned on 1.19.5: declared permission names first, then the rest."""
    order = ["read", "edit", "glob", "grep", "list", "bash", "task", "external_directory", "todowrite", "question",
             "webfetch", "websearch", "lsp", "doom_loop", "skill", "*", "codesearch"]  # fmt: skip
    return {key: permission[key] for key in order}


def _rule(permission: str, pattern: str, action: str) -> dict[str, str]:
    return {"permission": permission, "pattern": pattern, "action": action}


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


def test_a_tool_is_hidden_only_when_the_last_rule_for_its_permission_is_a_catch_all_deny() -> None:
    rules = enforced_rules(opencode_agents(_config())[0]["permission"]) or []
    assert hidden_from_model(rules, "bash") and hidden_from_model(rules, "task") and hidden_from_model(rules, "question")
    assert not hidden_from_model(rules, "edit")  # its last rule denies only */.devpilot/*
    assert not hidden_from_model(rules, "read") and not hidden_from_model(rules, "glob")
    assert not hidden_from_model([*rules, ("bash", "ls", "allow")], "bash")  # a narrower later rule makes it offered


# --- The managed configuration ------------------------------------------------------


def test_the_managed_config_passes_its_own_check() -> None:
    config = _config()
    assert config_problems(config) == []
    assert verify_effective_config(config, opencode_agents(config)) == []


def test_without_the_enforced_rules_the_check_fails_closed() -> None:
    problems = verify_effective_config(_config())
    assert problems and "GET /agent" in problems[0]


def test_the_managed_config_holds_no_credential_and_points_at_the_gateway() -> None:
    config = _config()
    options = config["provider"]["openai"]["options"]
    assert options["baseURL"] == GATEWAY
    assert options["apiKey"] == "devpilot-model-gateway"  # a placeholder; the gateway adds the real key
    assert config["enabled_providers"] == ["openai"]


def test_the_managed_config_denies_unknown_permissions_by_default() -> None:
    permission = _config()["permission"]
    assert next(iter(permission)) == "*" and permission["*"] == "deny"


def test_the_build_agent_carries_its_own_copy_of_the_rules() -> None:
    config = _config()
    build = config["agent"]["build"]["permission"]
    assert build == config["permission"] and build is not config["permission"]
    assert {config["agent"][name].get("disable") for name in ("plan", "general", "explore")} == {True}


def test_session_rules_match_the_managed_policy() -> None:
    rules = [(r["permission"], r["pattern"], r["action"]) for r in session_rules()]
    assert evaluate(rules, "bash", "python main.py") == "deny"
    assert evaluate(rules, "edit", "calculator.py") == "allow"
    assert evaluate(rules, "edit", ".opencode/plugin/x.js") == "deny"
    assert evaluate(rules, "read", ".env") == "deny"
    assert evaluate(rules, "something_new", "x") == "deny"


# --- GET /config: order-independent checks ----------------------------------------------


def test_reordered_config_permission_keys_pass_because_order_is_not_judged_there() -> None:
    # Regression for the Phase 4C C8 failure: the server returns these keys re-ordered.
    config = _config()
    config["permission"] = _reordered_like_the_server(config["permission"])
    config["agent"]["build"]["permission"] = _reordered_like_the_server(config["agent"]["build"]["permission"])
    assert config_problems(config) == []
    # Read last-match-wins, that order would deny editing project files: why /config cannot judge order.
    rules = [(r["permission"], r["pattern"], r["action"]) for r in opencode_rule_list(config["permission"])]
    assert evaluate(rules, "edit", "calculator.py") == "deny"


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
    problems = config_problems(_with(change))
    assert any(expected in p for p in problems), problems


def test_disabled_mcp_servers_are_accepted() -> None:
    assert config_problems(_with({"mcp": {"tool": {"type": "local", "enabled": False}}})) == []


def test_a_declared_value_that_differs_from_the_managed_one_is_caught() -> None:
    config = _config()
    config["permission"]["bash"] = "allow"
    config["agent"]["build"]["permission"]["edit"]["*.json"] = "allow"
    problems = config_problems(config)
    assert "global: the declared rule 'bash' differs from DevPilot's managed value" in problems
    assert "agent build: the declared rule 'edit' differs from DevPilot's managed value" in problems


def test_a_declared_key_devpilot_did_not_set_is_caught() -> None:
    config = _config()
    config["permission"]["b*sh"] = "allow"
    assert "global: the declared rules hold keys DevPilot did not set: b*sh" in config_problems(config)


def test_a_missing_declared_key_or_rule_object_is_caught() -> None:
    config = _config()
    del config["permission"]["*"]
    assert "global: the declared rules lack '*'" in config_problems(config)
    for value in (None, "allow", session_rules(), 42):
        problems = config_problems(_with({"permission": value}))
        assert "global: the declared permission rules are missing or not an object" in problems


def test_subagents_must_stay_disabled_and_no_other_agent_may_be_enabled() -> None:
    config = _config()
    config["agent"]["general"] = {"disable": False}
    config["agent"]["helper"] = {"permission": {"bash": "allow"}}
    problems = config_problems(config)
    assert "agent general must be disabled" in problems
    assert "agents DevPilot did not configure are enabled: helper" in problems


def test_a_config_without_a_build_agent_or_agent_map_is_caught() -> None:
    config = _config()
    del config["agent"]["build"]
    assert "the build agent is missing from the configuration" in config_problems(config)
    assert "agent configuration is missing or in an unknown format" in config_problems(_with({"agent": []}))


def test_a_config_that_is_not_an_object_is_refused() -> None:
    assert config_problems(["not", "an", "object"]) == ["the engine's configuration is not a JSON object"]


# --- GET /agent: the enforced, ordered rules ----------------------------------------------


def test_the_enforced_rules_of_the_managed_config_pass() -> None:
    agents = opencode_agents(_config())
    assert len(agents[0]["permission"]) == 101  # as on the real server
    assert agent_rule_problems(agents) == []


def test_a_malicious_later_allow_rule_is_caught() -> None:
    agents = opencode_agents(_config(), extra_rules=[_rule("bash", "*", "allow")])
    assert any(p.startswith("agent build (enforced rules): bash is not denied") for p in agent_rule_problems(agents))


def test_an_agent_specific_override_is_caught_even_when_the_declared_rules_look_right() -> None:
    config = _config()
    agents = opencode_agents(config, extra_rules=[_rule("edit", "*/opencode.json", "allow"), _rule("read", "*/.env", "allow")])
    assert config_problems(config) == []  # the declaration alone is not evidence
    problems = agent_rule_problems(agents)
    assert any("edit is not denied for '/workspace/opencode.json'" in p for p in problems), problems
    assert any("read is not denied for '/workspace/.env'" in p for p in problems), problems


def test_a_rule_that_blocks_project_edits_is_reported() -> None:
    agents = opencode_agents(_config(), extra_rules=[_rule("edit", "*", "deny")])
    assert any("edit must be allowed for project files" in p for p in agent_rule_problems(agents))


def test_a_missing_build_agent_is_caught() -> None:
    agents = [a for a in opencode_agents(_config()) if a["name"] != "build"]
    assert agent_rule_problems(agents) == ["the build agent is missing from GET /agent"]


def test_unexpected_permission_names_must_end_denied() -> None:
    # plan_enter and plan_exit appear on 1.19.5 and end denied through the catch-all.
    assert agent_rule_problems(opencode_agents(_config())) == []
    agents = opencode_agents(_config(), extra_rules=[_rule("pty", "*", "allow"), _rule("plan_enter", "*", "ask")])
    problems = agent_rule_problems(agents)
    assert any("unreviewed permission 'pty' is not denied" in p for p in problems)
    assert any("unreviewed permission 'plan_enter' is not denied" in p for p in problems)


def test_a_wildcard_permission_name_that_allows_is_caught() -> None:
    agents = opencode_agents(_config(), extra_rules=[_rule("b*sh", "*", "allow")])
    problems = agent_rule_problems(agents)
    assert any("wildcard permission rule ('b*sh', '*', 'allow')" in p for p in problems)
    assert any("bash is not denied" in p for p in problems)


def test_external_folders_may_be_allowed_only_for_opencodes_own_tool_output() -> None:
    assert not any("external_directory" in p for p in agent_rule_problems(opencode_agents(_config())))
    agents = opencode_agents(_config(), extra_rules=[_rule("external_directory", "/home/*", "allow")])
    assert any("external_directory is not denied for '/home/*'" in p for p in agent_rule_problems(agents))


def test_agents_devpilot_did_not_allow_duplicates_and_visible_system_agents_are_caught() -> None:
    agents = opencode_agents(_config())
    agents.append({"name": "general", "permission": []})
    agents.append({"name": "devpilot-hostile", "permission": []})
    agents.append(copy.deepcopy(agents[0]))
    agents[1]["hidden"] = False
    problems = agent_rule_problems(agents)
    assert "agents DevPilot did not allow are available: devpilot-hostile, general" in problems
    assert "GET /agent lists an agent more than once: build" in problems
    assert "system agents are not hidden: compaction" in problems


@pytest.mark.parametrize(
    "agents",
    [
        {"build": {"permission": []}},  # an object instead of a list
        [{"permission": []}],  # an agent without a name
        "build",
        None,
    ],
)
def test_unexpected_agent_structures_fail_closed(agents: Any) -> None:
    assert agent_rule_problems(agents) == ["GET /agent did not return a list of named agents"]


@pytest.mark.parametrize(
    "permission",
    [
        {"bash": "deny"},  # the object form is not what GET /agent returns
        [_rule("bash", "*", "deny"), "not a rule"],
        [{"permission": "bash", "pattern": "*"}],
        [_rule("bash", "*", "maybe")],
        [{"permission": 1, "pattern": "*", "action": "deny"}],
        None,
    ],
)
def test_malformed_enforced_rules_fail_closed(permission: Any) -> None:
    agents = opencode_agents(_config())
    agents[0]["permission"] = permission
    assert agent_rule_problems(agents) == ["agent build (enforced rules): the rule list is missing or malformed"]


def test_an_empty_rule_list_means_opencodes_allow_default_and_is_refused() -> None:
    assert any("bash is not denied" in p for p in ruleset_problems([], scope="build"))


def test_only_the_defaults_without_devpilots_rules_are_refused() -> None:
    assert any("bash is not denied" in p for p in ruleset_problems(OPENCODE_DEFAULT_RULES, scope="build"))


def test_the_combined_check_reports_both_sources() -> None:
    config = _config()
    config["lsp"] = True
    agents = opencode_agents(_config(), extra_rules=[_rule("bash", "*", "allow")])
    problems = verify_effective_config(config, agents)
    assert any("lsp must be false" in p for p in problems)
    assert any("bash is not denied" in p for p in problems)


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
