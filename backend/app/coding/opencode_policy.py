"""OpenCode's policy: the managed configuration DevPilot ships, and a check of the effective one.

Why both (SECURITY T3):
- OpenCode merges configuration from several places. A project `opencode.json` or a
  `.opencode/` folder in the workspace overrides the global file and can add plugins
  (JavaScript run by OpenCode), MCP servers (commands run by OpenCode), agents and permission
  rules. Only managed configuration (`/etc/opencode/` on Linux) cannot be overridden.
- One flag does not close every path. Permission rules are evaluated last-match-wins with
  wildcards, agents can carry their own rules, and LSP servers and formatters run programs.

So DevPilot (1) mounts `managed_config()` as the managed file, (2) refuses to start a task
unless `verify_effective_config()` finds the server's effective policy safe, and (3) never lets
a task keep an edit to the files that feed OpenCode's configuration (`is_protected`).

Where the effective policy is read (verified on the real 1.19.5 server, Phase 4C):
- `GET /agent` gives each agent's merged rule list in the order OpenCode enforces it. Rule
  order, and so every allow or deny decision, is judged only from there.
- `GET /config` re-orders permission keys, so it is checked only where order does not matter:
  the engine settings, the declared rules compared as key/value sets, and disabled agents.
"""

from __future__ import annotations

import re
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

PROMPTED_AGENT = "build"
# OpenCode's own hidden agents, listed by GET /agent on 1.19.5. They write titles and summaries
# and are never prompted by DevPilot.
SYSTEM_AGENTS = frozenset({"compaction", "summary", "title"})
# Permission names DevPilot has decided on. Any other name in the enforced rules (for example
# plan_enter and plan_exit on 1.19.5) must end denied.
REVIEWED_PERMISSIONS = frozenset({"read", "edit", *_ALLOWED, *_DENIED})
# OpenCode 1.19.5 appends an allow for its own tool-output folder after every configured rule. It
# lies in the container's RAM tmpfs (XDG_DATA_HOME=/tmp/home/.local/share, opencode_check.py).
ACCEPTED_EXTERNAL_ALLOWS = frozenset({"/tmp/home/.local/share/opencode/tool-output/*"})

# What the enforced rules must decide, checked on sample inputs. OpenCode matches edit and read
# rules against the path relative to the project's worktree; for /workspace without git that
# path is "workspace/<file>" on 1.19.5 (the session's `path` field), so those forms are sampled too.
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
        "opencode.json", "/workspace/opencode.json", "workspace/opencode.json",
        ".opencode/plugin/x.js", "/workspace/.opencode/plugin/x.js", "workspace/.opencode/plugin/x.js",
        "AGENTS.md", ".git/config", ".env", "sub/.env", "/workspace/.env", "workspace/.env",
    ),
    "read": (".env", "/workspace/.env", "workspace/.env"),
    "devpilot_future_permission": ("anything",),  # an unknown permission must fall to the catch-all deny
}  # fmt: skip
_MUST_ALLOW: dict[str, tuple[str, ...]] = {
    "edit": ("calculator.py", "/workspace/calculator.py", "workspace/calculator.py", "src/app/main.py"),
    "read": ("calculator.py", "/workspace/calculator.py", "workspace/calculator.py"),
}
_ACTIONS = frozenset({"allow", "ask", "deny"})

# The value of `apiKey`: OpenCode needs one to start, and DevPilot's model gateway ignores it.
GATEWAY_KEY_PLACEHOLDER = "devpilot-model-gateway"
# SDK packages the 1.19.5 binary bundles for the openai provider. Any other package name makes OpenCode
# install it from npm the first time the model is used (Npm.add, read from the binary).
BUNDLED_PROVIDER_PACKAGES = frozenset({"@ai-sdk/openai"})

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


def _managed_permission() -> dict[str, Any]:
    permission: dict[str, Any] = {"*": "deny"}
    permission["read"] = {"*": "allow", **{p: "deny" for p in _READ_DENY}}
    permission["edit"] = {"*": "allow", **{p: "deny" for p in _EDIT_DENY}}
    permission.update({name: "allow" for name in _ALLOWED})
    permission.update({name: "deny" for name in _DENIED})
    return permission


def managed_config(*, model: str, gateway_base_url: str) -> dict[str, Any]:
    """The configuration mounted read-only at /etc/opencode/opencode.json in the OpenCode container.

    The OpenAI key is not in it: OpenCode talks to DevPilot's model gateway, which adds the
    key outside the container (ARCHITECTURE §6). `apiKey` is a placeholder the gateway ignores.
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "small_model": model,
        "enabled_providers": ["openai"],
        "provider": {"openai": {"options": {"baseURL": gateway_base_url, "apiKey": GATEWAY_KEY_PLACEHOLDER}}},
        "autoupdate": False,
        "share": "disabled",
        "lsp": False,
        "formatter": False,
        "plugin": [],
        "mcp": {},
        "instructions": [],
        "permission": _managed_permission(),
        # Agent rules take precedence over global rules, and managed settings protect only the
        # keys they set. Pinning the build agent's rules here stops a project-level
        # `agent.build.permission` entry from widening them.
        "agent": {
            PROMPTED_AGENT: {"permission": _managed_permission()},
            **{name: {"disable": True} for name in _DISABLED_AGENTS},
        },
    }


def verify_effective_config(config: Any, agents: Any = None) -> list[str]:
    """Problems with the engine's effective policy. Empty means safe.

    `config` is the GET /config answer and `agents` the GET /agent answer. Without `agents` the
    result is never empty: the order of the rules cannot be judged from /config alone.
    """
    problems = config_problems(config)
    if agents is None:
        return ["the enforced agent rules (GET /agent) were not supplied: rule order cannot be judged from GET /config", *problems]
    return problems + agent_rule_problems(agents)


def config_problems(config: Any) -> list[str]:
    """Problems in GET /config that do not depend on key order. Allow and deny decisions are not judged here."""
    if not isinstance(config, dict):
        return ["the engine's configuration is not a JSON object"]
    problems = _declared(config.get("permission"), scope="global")
    agents = config.get("agent")
    if not isinstance(agents, dict):
        problems.append("agent configuration is missing or in an unknown format")
    else:
        build = agents.get(PROMPTED_AGENT)
        if not isinstance(build, dict):
            problems.append(f"the {PROMPTED_AGENT} agent is missing from the configuration")
        else:
            problems += _declared(build.get("permission"), scope=f"agent {PROMPTED_AGENT}")
        for name in _DISABLED_AGENTS:
            if not isinstance(agents.get(name), dict) or agents[name].get("disable") is not True:
                problems.append(f"agent {name} must be disabled")
        extra = sorted(
            str(name)
            for name, agent in agents.items()
            if name != PROMPTED_AGENT and name not in _DISABLED_AGENTS and not (isinstance(agent, dict) and agent.get("disable") is True)
        )
        if extra:
            problems.append(f"agents DevPilot did not configure are enabled: {', '.join(extra)}")

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
    return problems + install_source_problems(config)


def install_source_problems(config: dict[str, Any]) -> list[str]:
    """Configuration that would make OpenCode fetch something: a provider package or skills from a URL."""
    problems: list[str] = []
    providers = config.get("provider") or {}
    if not isinstance(providers, dict):
        return ["the provider configuration is not an object"]
    extra = sorted(str(name) for name in providers if name != "openai")
    if extra:
        problems.append(f"providers other than openai are configured: {', '.join(extra)}")
    packages = sorted(str(p) for p in _package_names(providers) if p not in BUNDLED_PROVIDER_PACKAGES)
    if packages:
        problems.append(f"provider packages OpenCode would install from npm: {', '.join(packages)}")
    skills = config.get("skills")
    if isinstance(skills, dict) and skills.get("urls"):
        problems.append("skills.urls must be empty: OpenCode downloads skills from those addresses")
    return problems


def _package_names(value: Any) -> list[Any]:
    """Every `npm` value at any depth: a provider, one of its models, or a model's own provider block."""
    if isinstance(value, dict):
        found = [value["npm"]] if "npm" in value else []
        return found + [name for item in value.values() for name in _package_names(item)]
    return [name for item in value for name in _package_names(item)] if isinstance(value, list) else []


def _declared(raw: Any, *, scope: str) -> list[str]:
    """The declared rules must equal DevPilot's managed rules as key/value sets; order is ignored."""
    if not isinstance(raw, dict):
        return [f"{scope}: the declared permission rules are missing or not an object"]
    expected = _managed_permission()
    problems = [f"{scope}: the declared rules lack {key!r}" for key in expected if key not in raw]
    problems += [
        f"{scope}: the declared rule {key!r} differs from DevPilot's managed value"
        for key, value in expected.items()
        if key in raw and raw[key] != value
    ]
    extra = sorted(str(key) for key in raw if key not in expected)
    if extra:
        problems.append(f"{scope}: the declared rules hold keys DevPilot did not set: {', '.join(extra)}")
    return problems


def agent_rule_problems(agents: Any, *, agent: str = PROMPTED_AGENT) -> list[str]:
    """Problems with GET /agent, judged on the ordered rules OpenCode enforces for `agent`."""
    if not isinstance(agents, list) or not all(isinstance(a, dict) and isinstance(a.get("name"), str) for a in agents):
        return ["GET /agent did not return a list of named agents"]
    names = [a["name"] for a in agents]
    problems: list[str] = []
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        problems.append(f"GET /agent lists an agent more than once: {', '.join(duplicates)}")
    unexpected = sorted(set(names) - {agent} - SYSTEM_AGENTS)
    if unexpected:
        problems.append(f"agents DevPilot did not allow are available: {', '.join(unexpected)}")
    visible = sorted(a["name"] for a in agents if a["name"] in SYSTEM_AGENTS and a.get("hidden") is not True)
    if visible:
        problems.append(f"system agents are not hidden: {', '.join(visible)}")
    entry = next((a for a in agents if a["name"] == agent), None)
    if entry is None:
        problems.append(f"the {agent} agent is missing from GET /agent")
        return problems
    return problems + ruleset_problems(entry.get("permission"), scope=f"agent {agent} (enforced rules)")


def ruleset_problems(raw: Any, *, scope: str) -> list[str]:
    """Problems with one ordered rule list as OpenCode enforces it (an agent's rules from GET /agent)."""
    rules = enforced_rules(raw)
    if rules is None:
        return [f"{scope}: the rule list is missing or malformed"]
    problems = _check_rules(rules, scope=scope)
    for name in sorted({p for p, _, _ in rules if not _has_wildcard(p)} - REVIEWED_PERMISSIONS):
        if evaluate(rules, name, "anything") != "deny":
            problems.append(f"{scope}: the unreviewed permission {name!r} is not denied")
    for permission, pattern, action in rules:
        if permission != "*" and _has_wildcard(permission) and action != "deny":
            problems.append(f"{scope}: a wildcard permission rule ({permission!r}, {pattern!r}, {action!r}) is not allowed")
        if permission == "external_directory" and action != "deny" and pattern not in ACCEPTED_EXTERNAL_ALLOWS:
            if evaluate(rules, permission, pattern.replace("*", "x").replace("?", "x")) != "deny":
                problems.append(f"{scope}: external_directory is not denied for {pattern!r}")
    return problems


def enforced_rules(raw: Any) -> list[Rule] | None:
    """The list form GET /agent uses: [{permission, pattern, action}, ...] with known actions. None if malformed."""
    if not isinstance(raw, list):
        return None
    rules: list[Rule] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        permission, pattern, action = item.get("permission"), item.get("pattern"), item.get("action")
        if not (isinstance(permission, str) and isinstance(pattern, str) and action in _ACTIONS):
            return None
        rules.append((permission, pattern, str(action)))
    return rules


def hidden_from_model(rules: list[Rule], permission: str) -> bool:
    """OpenCode 1.19.5 offers a tool to the model unless the last rule for its permission is `* deny`."""
    last = next((r for r in reversed(rules) if _wildcard(r[0]).fullmatch(permission)), None)
    return last is not None and last[1] == "*" and last[2] == "deny"


def evaluate(rules: list[Rule] | tuple[Rule, ...], permission: str, value: str) -> str:
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


def _has_wildcard(text: str) -> bool:
    return "*" in text or "?" in text


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
FORBIDDEN_TOOLS = frozenset({"bash", "shell", "webfetch", "websearch", "codesearch", "task", "skill", "question"})
