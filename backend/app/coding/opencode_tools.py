"""Which OpenCode tools may run, and what they may touch (Phase 4D Stage 6, SECURITY T3).

This is an allowlist. A tool that is not listed is refused, whatever its name, so a tool that a
later release adds, or that a plugin or an MCP server provides, stays refused until it is reviewed.

Reviewed against the pinned 1.19.5 binary, read as bytes and never run:

    Tool         Permission   What it does
    read         read         reads a file
    glob         glob         lists paths; runs ripgrep (see INSTALL_PATHS in opencode_container.py)
    grep         grep         searches files; runs ripgrep
    edit         edit         replaces text in one file
    write        edit         creates or replaces one file
    apply_patch  edit         adds, updates, moves and deletes several files in one call
    todowrite    todowrite    keeps a task list in OpenCode's own database; touches no project file
    invalid      (none)       see below

`invalid` executes nothing. It is OpenCode's handler for a malformed tool call: when the model names
a tool that does not exist, or a hidden one, `experimental_repairToolCall` rewrites the call to
`invalid`, whose `execute` only returns the text "The arguments provided to the tool are invalid"
with no side effect. It asks no permission, and `activeTools` leaves it out, so the model is never
offered it. It stays allowed because refusing it would stop a task over a model's typing mistake
while nothing has happened.

So a model that asks for a hidden tool produces a `pending` part under that name, which the
repair then turns into `invalid`: nothing ran. Only the `tool-call` step sets a part's name for
execution, so a part in any later state under a tool that is not allowed means the policy did not
hold. `tool_problems` judges exactly that: `pending` says what was asked for, not what ran.

`apply_patch` asks the `edit` permission only for each change's source path. A move's destination
is checked only for leaving the project, so OpenCode permitting a move says nothing about where it
lands. `file_operations` therefore reads the patch itself, and each destination is judged like any
other path.

Reading an event is a tripwire, not a control. The tool may already have written by the time its
event arrives. The audit of the real files after a task (audit.audit) is the authority on what
changed, and it is not a substitute for isolation either: it runs afterwards, on a private copy, in
a container that has no network and cannot see anything else.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.coding.opencode_contract import inside_workspace
from app.coding.opencode_policy import PROMPTED_AGENT, Rule, enforced_rules, evaluate, hidden_from_model, is_protected

# Tool id → the permission OpenCode 1.19.5 checks before the tool acts. `write` and `apply_patch` also
# ask "external_directory" for a path outside the project. A tool whose last rule for its permission is
# `* deny` is not offered to the model at all (`hidden_from_model`).
TOOL_PERMISSIONS: dict[str, str | None] = {
    "read": "read",
    "glob": "glob",
    "grep": "grep",
    "edit": "edit",
    "write": "edit",
    "apply_patch": "edit",
    "todowrite": "todowrite",
    "invalid": None,
    "bash": "bash",
    "task": "task",
    "question": "question",
    "webfetch": "webfetch",
    "websearch": "websearch",
    "skill": "skill",
    "lsp": "lsp",
    "codesearch": "codesearch",
}
CODING_TOOLS = frozenset({"read", "glob", "grep", "edit", "write", "apply_patch", "todowrite"})
INERT_TOOLS = frozenset({"invalid"})  # executes nothing; see the module's description
ALLOWED_TOOLS = CODING_TOOLS | INERT_TOOLS
WRITING_TOOLS = frozenset({"edit", "write", "apply_patch"})
# What GET /experimental/tool/ids lists on each reviewed release. Any other release is unreviewed.
PINNED_TOOL_IDS: dict[str, frozenset[str]] = {
    "1.19.5": frozenset(
        {"apply_patch", "bash", "edit", "glob", "grep", "invalid", "question", "read", "skill", "task", "todowrite", "webfetch", "websearch", "write"}
    ),
}

_PATH_INPUTS = {
    "read": ("filePath", "read"),
    "edit": ("filePath", "edit"),
    "write": ("filePath", "write"),
    "glob": ("path", "search"),
    "grep": ("path", "search"),
}
# The markers the binary's patch parser reads, each at the start of a line. Scanning every line for
# them finds at least everything the parser would: `*** ` cannot begin a line of file content.
_PATCH_MARKERS = (("*** Add File:", "create"), ("*** Update File:", "update"), ("*** Delete File:", "delete"), ("*** Move to:", "move"))
# A value each allowed tool's permission must allow for ordinary project work.
_TOOL_SAMPLES = {"read": "workspace/calculator.py", "edit": "workspace/calculator.py", "glob": "**/*.py",
                 "grep": "def add", "todowrite": "*"}  # fmt: skip


@dataclass(frozen=True)
class FileOperation:
    kind: str  # read, search, write, edit, create, update, move or delete
    path: str  # as the tool was given it: relative to the session's folder, or absolute


def file_operations(tool: str, state: Mapping[str, Any]) -> list[FileOperation]:
    """Every path one tool call names, from its input and, once it finished, from what it reported."""
    inputs = state.get("input") if isinstance(state.get("input"), Mapping) else {}
    if tool == "apply_patch":
        return _patch_operations(inputs.get("patchText")) + _patch_results(state.get("metadata"))
    key, kind = _PATH_INPUTS.get(tool, ("", ""))
    target = inputs.get(key)
    return [FileOperation(kind, target)] if isinstance(target, str) and target else []


def _patch_operations(text: Any) -> list[FileOperation]:
    if not isinstance(text, str):
        return []
    found: list[FileOperation] = []
    for raw in text.splitlines():
        line = raw.strip()
        for marker, kind in _PATCH_MARKERS:
            if line.startswith(marker) and (path := line[len(marker) :].strip()):
                found.append(FileOperation(kind, path))
    return found


def _patch_results(metadata: Any) -> list[FileOperation]:
    """The paths apply_patch reports when it finishes: `metadata.files[].filePath` and `.movePath`."""
    files = metadata.get("files") if isinstance(metadata, Mapping) else None
    found: list[FileOperation] = []
    for item in files if isinstance(files, list) else []:
        if isinstance(item, Mapping):
            if isinstance(item.get("filePath"), str):
                found.append(FileOperation(str(item.get("type") or "update"), item["filePath"]))
            if isinstance(item.get("movePath"), str):
                found.append(FileOperation("move", item["movePath"]))
    return found


def relative_path(path: str, workspace: str) -> str | None:
    """`path` relative to the workspace, "" for the workspace itself, None when it lies outside."""
    root = workspace.rstrip("/")
    cleaned = path.replace("\\", "/")
    normalized = posixpath.normpath(cleaned if cleaned.startswith("/") else f"{root}/{cleaned}")
    if normalized == root:
        return ""
    return normalized[len(root) + 1 :] if inside_workspace(normalized, root) else None


def tool_problems(tool: str, state: Mapping[str, Any], workspace: str) -> list[str]:
    """Why this tool call breaks DevPilot's policy. Judged from `running` on, including `error`: a denied
    attempt on a protected path is still an attempt. A `pending` part only says what the model asked for."""
    if state.get("status") == "pending":
        return []
    if tool not in ALLOWED_TOOLS:
        return [f"the engine called the tool {tool!r}, which DevPilot does not allow"]
    problems: list[str] = []
    for operation in file_operations(tool, state):
        relative = relative_path(operation.path, workspace)
        if relative is None:
            problems.append(f"the engine's {tool} tool targeted {operation.path[:100]!r}, outside the project folder")
        elif tool in WRITING_TOOLS and relative and is_protected(relative):
            problems.append(f"the engine's {tool} tool ({operation.kind}) targeted the protected path {relative[:100]!r}")
    return problems


def tool_rule_problems(ids: Iterable[str], rules: list[Rule]) -> list[str]:
    """Each listed tool must be treated as the review decided: offered and allowed, or denied and hidden."""
    wrong: list[str] = []
    for tool in sorted(set(ids)):
        permission = TOOL_PERMISSIONS.get(tool)
        if permission is None:
            continue  # `invalid` asks nothing and changes nothing; unreviewed ids are reported apart
        hidden = hidden_from_model(rules, permission)
        if tool in ALLOWED_TOOLS:
            if hidden or evaluate(rules, permission, _TOOL_SAMPLES.get(permission, "anything")) != "allow":
                wrong.append(f"{tool} ({permission}) should be offered and allowed")
        elif not hidden or evaluate(rules, permission, "anything") != "deny":
            wrong.append(f"{tool} ({permission}) should be denied and hidden from the model")
    return wrong


def tool_inventory_problems(ids: Iterable[str], version: str, agents: Any) -> list[str]:
    """Problems with the server's tools: not the pinned list for this release, or not treated as reviewed."""
    listed = set(ids)
    pinned = PINNED_TOOL_IDS.get(version)
    if pinned is None:
        return [f"the tools of OpenCode {version} have not been reviewed"]
    problems = [f"the server lists a tool that was not reviewed: {tool}" for tool in sorted(listed - pinned)]
    problems += [f"the server lacks a reviewed tool: {tool}" for tool in sorted(pinned - listed)]
    entries = agents if isinstance(agents, list) else []
    build = next((a for a in entries if isinstance(a, dict) and a.get("name") == PROMPTED_AGENT), None)
    rules = enforced_rules(build.get("permission")) if build else None
    if rules is None:
        return [*problems, "no enforced rules to compare the tools with"]
    return problems + tool_rule_problems(listed, rules)
