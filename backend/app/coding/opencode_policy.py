"""OpenCode's policy: the managed configuration DevPilot ships, and a check of the effective one.

Why both (SECURITY T3):
- OpenCode merges configuration from several places. A project `opencode.json` or a
  `.opencode/` folder in the workspace overrides the global file and can add plugins
  (JavaScript run by OpenCode), MCP servers (commands run by OpenCode), agents and permission
  rules. Only managed configuration (`/etc/opencode/` on Linux) cannot be overridden.
- One flag does not close every path. Permission rules are evaluated last-match-wins with
  wildcards, agents can carry their own rules, and LSP servers and formatters run programs.

So DevPilot (1) mounts `managed_config()` as the managed file, (2) refuses to start a task
unless `verify_effective_config()` finds the merged configuration safe, and (3) never lets
a task keep an edit to the files that feed OpenCode's configuration (`is_protected`).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

# Paths a coding task may never create, change or delete. Matched per path component,
# case-insensitively (the workspace lives on Windows, where case does not distinguish files).
PROTECTED_NAMES = frozenset({"opencode.json", "opencode.jsonc", "agents.md", "claude.md"})
PROTECTED_DIRS = frozenset({".opencode", ".git"})
RESERVED_TOP_DIRS = frozenset({".devpilot"})  # DevPilot's own files (dependencies, test reports)

# Permission rules for the managed configuration. A catch-all deny comes first, so any
# permission OpenCode adds in a later version is denied until DevPilot allows it.
_EDIT_DENY = (
    "opencode.json", "*/opencode.json", "opencode.jsonc", "*/opencode.jsonc",
    ".opencode", ".opencode/*", "*/.opencode", "*/.opencode/*",
    "AGENTS.md", "*/AGENTS.md", "CLAUDE.md", "*/CLAUDE.md",
    ".git", ".git/*", "*/.git", "*/.git/*",
    ".env", ".env.*", "*/.env", "*/.env.*",
    ".devpilot/*", "*/.devpilot/*",
)  # fmt: skip
_READ_DENY = (".env", ".env.*", "*/.env", "*/.env.*")
_DENIED = (
    "bash", "webfetch", "websearch", "codesearch", "external_directory",
    "task", "skill", "question", "doom_loop", "lsp",
)  # fmt: skip
_ALLOWED = ("glob", "grep", "list", "todowrite")
# Agents other than `build` are switched off; the hidden system agents keep working.
_DISABLED_AGENTS = ("plan", "general", "explore")

# What the effective configuration must decide, checked on sample inputs.
_MUST_DENY: dict[str, tuple[str, ...]] = {
    "bash": ("ls", "python main.py", "rm -rf /", "curl https://example.com", "sh -c id"),
    "webfetch": ("https://example.com",),
    "websearch": ("anything",),
    "codesearch": ("anything",),
    "external_directory": ("/etc/passwd", "/root/.ssh/id_rsa", "/tmp/x", "C:\\Windows\\system.ini"),
    "task": ("general", "explore", "build"),
    "skill": ("anything",),
    "question": ("anything",),
    "doom_loop": ("anything",),
    "edit": (
        "opencode.json", "/workspace/opencode.json", ".opencode/plugin/x.js", "/workspace/.opencode/plugin/x.js",
        "AGENTS.md", ".git/config", ".env", "sub/.env", "/workspace/.env",
    ),
    "read": (".env", "/workspace/.env"),
    "devpilot_future_permission": ("anything",),  # an unknown permission must fall to the catch-all deny
}  # fmt: skip
_MUST_ALLOW: dict[str, tuple[str, ...]] = {
    "edit": ("calculator.py", "/workspace/calculator.py", "src/app/main.py"),
    "read": ("calculator.py", "/workspace/calculator.py"),
}

Rule = tuple[str, str, str]  # (permission, pattern, action)


def is_protected(rel_path: str) -> bool:
    """True for paths that feed OpenCode's configuration or belong to DevPilot."""
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return False
    lowered = [p.casefold() for p in parts]
    if lowered[0] in RESERVED_TOP_DIRS:
        return True
    if any(p in PROTECTED_DIRS for p in lowered):
        return True
    name = lowered[-1]
    if name in PROTECTED_NAMES:
        return True
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def managed_config(*, model: str, gateway_base_url: str) -> dict[str, Any]:
    """The configuration mounted read-only at /etc/opencode/opencode.json in the OpenCode container.

    The OpenAI key is not in it: OpenCode talks to DevPilot's model gateway, which adds the
    key outside the container (ARCHITECTURE §6). `apiKey` is a placeholder the gateway ignores.
    """
    permission: dict[str, Any] = {"*": "deny"}
    permission["read"] = {"*": "allow", **{p: "deny" for p in _READ_DENY}}
    permission["edit"] = {"*": "allow", **{p: "deny" for p in _EDIT_DENY}}
    permission.update({name: "allow" for name in _ALLOWED})
    permission.update({name: "deny" for name in _DENIED})
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "small_model": model,
        "enabled_providers": ["openai"],
        "provider": {"openai": {"options": {"baseURL": gateway_base_url, "apiKey": "devpilot-model-gateway"}}},
        "autoupdate": False,
        "share": "disabled",
        "lsp": False,
        "formatter": False,
        "plugin": [],
        "mcp": {},
        "instructions": [],
        "permission": permission,
        # Agent rules take precedence over global rules, and managed settings protect only the
        # keys they set. Pinning the build agent's rules here stops a project-level
        # `agent.build.permission` entry from widening them.
        "agent": {"build": {"permission": copy.deepcopy(permission)}, **{name: {"disable": True} for name in _DISABLED_AGENTS}},
    }


def verify_effective_config(config: Any) -> list[str]:
    """Problems with the configuration OpenCode reports (GET /config). Empty means safe."""
    if not isinstance(config, dict):
        return ["the engine's configuration is not a JSON object"]
    problems: list[str] = []
    rules = _rules(config.get("permission"))
    if rules is None:
        problems.append("permission rules are missing or in an unknown format")
        rules = []
    problems.extend(_check_rules(rules, scope="global"))

    agents = config.get("agent") or {}
    if not isinstance(agents, dict):
        problems.append("agent configuration is in an unknown format")
        agents = {}
    for name, agent in sorted(agents.items()):
        if not isinstance(agent, dict) or agent.get("disable") is True:
            continue
        agent_rules = _rules(agent.get("permission")) if "permission" in agent else []
        if agent_rules is None:
            problems.append(f"agent {name}: permission rules are in an unknown format")
            continue
        # Agent rules come after the global ones, so they win where both match.
        problems.extend(_check_rules([*rules, *agent_rules], scope=f"agent {name}"))

    if config.get("lsp") is not False:
        problems.append("lsp must be false: language servers are programs OpenCode would download and run")
    if config.get("formatter") is not False:
        problems.append("formatter must be false: formatters are programs OpenCode runs on saved files")
    if config.get("plugin"):
        problems.append("plugins must be empty: a plugin is code OpenCode runs")
    mcp = config.get("mcp") or {}
    if not isinstance(mcp, dict) or any(not isinstance(s, dict) or s.get("enabled") is not False for s in mcp.values()):
        problems.append("MCP servers must be absent or disabled: a local MCP server is a command OpenCode runs")
    if config.get("share") != "disabled":
        problems.append('share must be "disabled": sharing uploads sessions to a public service')
    if config.get("autoupdate") is not False:
        problems.append("autoupdate must be false")
    providers = config.get("enabled_providers")
    if not isinstance(providers, list) or not providers or any(p != "openai" for p in providers):
        problems.append('enabled_providers must be ["openai"]')
    return problems


def evaluate(rules: Iterable[Rule], permission: str, value: str) -> str:
    """OpenCode's rule: the last rule whose permission and pattern both match wins. Unmatched means allow."""
    action = "allow"
    for rule_permission, pattern, rule_action in rules:
        if _wildcard(rule_permission).fullmatch(permission) and _wildcard(pattern).fullmatch(value):
            action = rule_action
    return action


def _check_rules(rules: list[Rule], *, scope: str) -> list[str]:
    problems: list[str] = []
    for permission, samples in _MUST_DENY.items():
        allowed = [s for s in samples if evaluate(rules, permission, s) != "deny"]
        if allowed:
            problems.append(f"{scope}: {permission} is not denied for {', '.join(repr(s) for s in allowed[:3])}")
    for permission, samples in _MUST_ALLOW.items():
        blocked = [s for s in samples if evaluate(rules, permission, s) != "allow"]
        if blocked:
            problems.append(f"{scope}: {permission} must be allowed for project files such as {blocked[0]!r}")
    return problems


def _rules(raw: Any) -> list[Rule] | None:
    """Normalize the object form ({"bash": "deny", "edit": {"*": "allow"}}) and the list form."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [("*", "*", raw)] if raw in ("allow", "ask", "deny") else None
    rules: list[Rule] = []
    if isinstance(raw, dict):
        for permission, value in raw.items():
            if isinstance(value, str):
                rules.append((str(permission), "*", value))
            elif isinstance(value, dict):
                rules.extend((str(permission), str(pattern), str(action)) for pattern, action in value.items())
            else:
                return None
        return rules
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict) or not {"permission", "pattern", "action"} <= item.keys():
                return None
            rules.append((str(item["permission"]), str(item["pattern"]), str(item["action"])))
        return rules
    return None


@lru_cache(maxsize=512)
def _wildcard(pattern: str) -> re.Pattern[str]:
    # OpenCode's syntax: `*` is any run of characters, `?` is one character, the rest is literal.
    return re.compile("".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern), re.DOTALL)


def session_rules() -> list[dict[str, str]]:
    """The same denials, sent with every new session as a further layer (list form)."""
    rules = [{"permission": "*", "pattern": "*", "action": "deny"}]
    rules += [{"permission": "read", "pattern": "*", "action": "allow"}]
    rules += [{"permission": "read", "pattern": p, "action": "deny"} for p in _READ_DENY]
    rules += [{"permission": "edit", "pattern": "*", "action": "allow"}]
    rules += [{"permission": "edit", "pattern": p, "action": "deny"} for p in _EDIT_DENY]
    rules += [{"permission": name, "pattern": "*", "action": "allow"} for name in _ALLOWED]
    rules += [{"permission": name, "pattern": "*", "action": "deny"} for name in _DENIED]
    return rules


# Tools that must never run. A completed call to one of these means the policy did not hold.
FORBIDDEN_TOOLS = frozenset({"bash", "shell", "webfetch", "websearch", "codesearch", "task", "skill"})
