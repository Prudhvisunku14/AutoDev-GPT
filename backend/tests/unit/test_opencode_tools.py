"""The tool allowlist, the pinned inventory, and what each tool may touch. Pure functions on plain values."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.coding.audit import audit, take_snapshot
from app.coding.opencode_policy import enforced_rules, managed_config
from app.coding.opencode_tools import (
    ALLOWED_TOOLS,
    CODING_TOOLS,
    INERT_TOOLS,
    PINNED_TOOL_IDS,
    TOOL_PERMISSIONS,
    WRITING_TOOLS,
    FileOperation,
    file_operations,
    relative_path,
    tool_inventory_problems,
    tool_problems,
    tool_rule_problems,
)
from tests.helpers import OPENCODE_1_19_5_TOOLS, opencode_agents

WORKSPACE = "/workspace"
PINNED = "1.19.5"


def _running(**inputs: Any) -> dict[str, Any]:
    return {"status": "running", "input": inputs}


def _patch(*lines: str) -> dict[str, Any]:
    return _running(patchText="\n".join(("*** Begin Patch", *lines, "*** End Patch")))


def _agents() -> list[dict[str, Any]]:
    return opencode_agents(managed_config(model="openai/x", gateway_base_url="http://gw.invalid/v1"))


# --- The allowlist ----------------------------------------------------------------------------


def test_the_allowlist_is_exactly_the_reviewed_tools() -> None:
    assert CODING_TOOLS == {"read", "glob", "grep", "edit", "write", "apply_patch", "todowrite"}
    assert INERT_TOOLS == {"invalid"}
    assert ALLOWED_TOOLS == CODING_TOOLS | INERT_TOOLS


@pytest.mark.parametrize("tool", sorted(ALLOWED_TOOLS))
def test_each_allowed_tool_may_run_on_a_project_file(tool: str) -> None:
    assert tool_problems(tool, _running(filePath="src/main.py", path="src"), WORKSPACE) == []
    assert tool_problems(tool, {"status": "completed", "input": {}}, WORKSPACE) == []


@pytest.mark.parametrize(
    "tool",
    ["bash", "shell", "pty", "interactive_shell", "webfetch", "websearch", "codesearch", "task", "skill", "question", "lsp",
     "plan_exit", "multiedit", "patch", "list", "mcp_server_tool", "github_create_issue", "some_future_tool", "unknown", "", "BASH", "Read"],
)  # fmt: skip
@pytest.mark.parametrize("status", ["running", "completed", "error", "something-new"])
def test_anything_else_is_refused_once_it_goes_past_being_asked_for(tool: str, status: str) -> None:
    problems = tool_problems(tool, {"status": status, "input": {}}, WORKSPACE)
    assert len(problems) == 1 and "does not allow" in problems[0]


def test_a_state_with_no_status_is_judged_rather_than_excused() -> None:
    assert tool_problems("bash", {}, WORKSPACE)


def test_asking_for_a_hidden_tool_ran_nothing() -> None:
    # 1.19.5 opens a pending part under the requested name; the repair then turns the call into `invalid`.
    assert tool_problems("bash", {"status": "pending", "input": {}, "raw": ""}, WORKSPACE) == []
    assert tool_problems("invalid", _running(tool="bash", error="no such tool"), WORKSPACE) == []


def test_every_listed_tool_has_a_reviewed_permission() -> None:
    assert {TOOL_PERMISSIONS[t] for t in ALLOWED_TOOLS} == {"read", "glob", "grep", "edit", "todowrite", None}
    assert [t for t in WRITING_TOOLS if TOOL_PERMISSIONS[t] != "edit"] == []  # the binary: all three ask "edit"
    assert TOOL_PERMISSIONS["invalid"] is None  # it asks nothing and does nothing
    assert all(TOOL_PERMISSIONS[t] == t for t in TOOL_PERMISSIONS.keys() - ALLOWED_TOOLS)
    assert PINNED_TOOL_IDS[PINNED] <= TOOL_PERMISSIONS.keys()


# --- The inventory ----------------------------------------------------------------------------


def test_the_pinned_inventory_is_what_the_real_server_lists() -> None:
    assert PINNED_TOOL_IDS[PINNED] == set(OPENCODE_1_19_5_TOOLS)


def test_the_real_inventory_passes() -> None:
    assert tool_inventory_problems(OPENCODE_1_19_5_TOOLS, PINNED, _agents()) == []


@pytest.mark.parametrize("version", ["1.19.4", "1.19.6", "1.20.0", "", "2.0.0"])
def test_an_unreviewed_release_is_refused_whatever_it_lists(version: str) -> None:
    problems = tool_inventory_problems(OPENCODE_1_19_5_TOOLS, version, _agents())
    assert problems == [f"the tools of OpenCode {version} have not been reviewed"]


@pytest.mark.parametrize("extra", ["pty_exec", "mcp_tool", "lsp", "codesearch", "multiedit"])
def test_an_extra_tool_is_refused_even_if_a_rule_would_deny_it(extra: str) -> None:
    problems = tool_inventory_problems([*OPENCODE_1_19_5_TOOLS, extra], PINNED, _agents())
    assert f"the server lists a tool that was not reviewed: {extra}" in problems


def test_a_missing_tool_is_reported() -> None:
    listed = [t for t in OPENCODE_1_19_5_TOOLS if t != "apply_patch"]
    assert "the server lacks a reviewed tool: apply_patch" in tool_inventory_problems(listed, PINNED, _agents())


@pytest.mark.parametrize("agents", [None, [], {}, [{"name": "other", "permission": []}], [{"name": "build", "permission": "x"}]])
def test_the_inventory_fails_closed_without_enforced_rules(agents: Any) -> None:
    assert "no enforced rules to compare the tools with" in tool_inventory_problems(OPENCODE_1_19_5_TOOLS, PINNED, agents)


def test_the_inventory_compares_every_tool_with_the_enforced_rules() -> None:
    managed = managed_config(model="openai/x", gateway_base_url="http://gw.invalid/v1")
    rules = [{"permission": "bash", "pattern": "ls", "action": "allow"}, {"permission": "edit", "pattern": "*", "action": "deny"}]
    problems = tool_inventory_problems(OPENCODE_1_19_5_TOOLS, PINNED, opencode_agents(managed, extra_rules=rules))
    assert "bash (bash) should be denied and hidden from the model" in problems
    assert "apply_patch (edit) should be offered and allowed" in problems


def test_rules_that_treat_a_tool_differently_from_the_review_are_refused() -> None:
    build = next(a for a in _agents() if a["name"] == "build")
    rules = enforced_rules(build["permission"])
    assert rules is not None and tool_rule_problems(OPENCODE_1_19_5_TOOLS, rules) == []
    wrong = tool_rule_problems(OPENCODE_1_19_5_TOOLS, [*rules, ("bash", "ls", "allow"), ("edit", "*", "deny")])
    assert "bash (bash) should be denied and hidden from the model" in wrong
    assert "apply_patch (edit) should be offered and allowed" in wrong


# --- What each tool names ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "inputs", "expected"),
    [
        ("read", {"filePath": "a.py"}, [FileOperation("read", "a.py")]),
        ("glob", {"pattern": "*.py", "path": "src"}, [FileOperation("search", "src")]),
        ("glob", {"pattern": "*.py"}, []),
        ("grep", {"pattern": "x", "path": "src"}, [FileOperation("search", "src")]),
        ("edit", {"filePath": "a.py", "oldString": "x", "newString": "y"}, [FileOperation("edit", "a.py")]),
        ("write", {"filePath": "b.py", "content": "x"}, [FileOperation("write", "b.py")]),
        ("todowrite", {"todos": []}, []),
        ("invalid", {"tool": "bash", "error": "x"}, []),
    ],
)
def test_each_tool_names_its_paths(tool: str, inputs: dict[str, Any], expected: list[FileOperation]) -> None:
    assert file_operations(tool, {"status": "running", "input": inputs}) == expected


@pytest.mark.parametrize("state", [{}, {"input": None}, {"input": "x"}, {"input": {"filePath": 7}}, {"input": {"filePath": ""}}])
def test_a_malformed_input_names_nothing(state: dict[str, Any]) -> None:
    assert file_operations("write", state) == []


def test_apply_patch_with_several_files_names_every_one() -> None:
    state = _patch(
        "*** Add File: new.py", "+print(1)",
        "*** Update File: old.py", "@@", "-a", "+b",
        "*** Update File: src/from.py", "*** Move to: src/to.py", "@@", "-a", "+b",
        "*** Delete File: gone.py",
    )  # fmt: skip
    assert file_operations("apply_patch", state) == [
        FileOperation("create", "new.py"), FileOperation("update", "old.py"), FileOperation("update", "src/from.py"),
        FileOperation("move", "src/to.py"), FileOperation("delete", "gone.py"),
    ]  # fmt: skip
    assert tool_problems("apply_patch", state, WORKSPACE) == []


def test_the_paths_apply_patch_reports_are_read_too() -> None:
    metadata = {"files": [
        {"filePath": "/workspace/a.py", "type": "update"},
        {"filePath": "/workspace/b.py", "type": "move", "movePath": "/workspace/c.py"},
        "not-an-entry",
    ]}  # fmt: skip
    state = {"status": "completed", "input": {}, "metadata": metadata}
    assert file_operations("apply_patch", state) == [
        FileOperation("update", "/workspace/a.py"), FileOperation("move", "/workspace/b.py"), FileOperation("move", "/workspace/c.py"),
    ]  # fmt: skip


def test_windows_line_endings_and_a_wrapped_patch_are_read_the_same() -> None:
    text = "cat <<'EOF'\r\n*** Begin Patch\r\n*** Update File: a.py\r\n*** Move to: opencode.json\r\n@@\r\n*** End Patch\r\nEOF"
    assert FileOperation("move", "opencode.json") in file_operations("apply_patch", _running(patchText=text))


def test_a_patch_that_is_not_text_names_nothing() -> None:
    assert file_operations("apply_patch", _running(patchText=None)) == []
    assert file_operations("apply_patch", {"metadata": "x"}) == []


# --- Protected paths, across every kind of file operation --------------------------------------

PROTECTED = ["opencode.json", "sub/opencode.jsonc", ".opencode/plugin/x.js", "AGENTS.md", "docs/CLAUDE.md", ".git/config", ".env", ".devpilot/x"]


@pytest.mark.parametrize("path", PROTECTED)
@pytest.mark.parametrize(
    ("tool", "state"),
    [
        ("write", lambda p: _running(filePath=p, content="x")),
        ("write", lambda p: _running(filePath=f"/workspace/{p}", content="x")),
        ("edit", lambda p: _running(filePath=p, oldString="a", newString="b")),
        ("apply_patch", lambda p: _patch(f"*** Add File: {p}", "+x")),
        ("apply_patch", lambda p: _patch(f"*** Update File: {p}", "@@", "-a", "+b")),
        ("apply_patch", lambda p: _patch(f"*** Delete File: {p}")),
        ("apply_patch", lambda p: _patch(f"*** Update File: {p}", f"*** Move to: elsewhere.py", "@@", "-a", "+b")),
    ],
)  # fmt: skip
def test_every_way_of_writing_a_protected_path_is_refused(tool: str, state: Any, path: str) -> None:
    problems = tool_problems(tool, state(path), WORKSPACE)
    assert any("protected path" in p for p in problems), problems


@pytest.mark.parametrize("path", PROTECTED)
def test_a_move_destination_is_judged_itself(path: str) -> None:
    # OpenCode asks the edit permission only for `src.py`, so its rules would allow this move.
    state = _patch("*** Update File: src.py", f"*** Move to: {path}", "@@", "-a", "+b")
    problems = tool_problems("apply_patch", state, WORKSPACE)
    assert len(problems) == 1 and "(move)" in problems[0] and "protected path" in problems[0]


def test_a_reported_move_destination_is_judged_when_the_patch_text_is_gone() -> None:
    state = {"status": "completed", "input": {}, "metadata": {"files": [{"filePath": "/workspace/a.py", "type": "move", "movePath": "/workspace/.env"}]}}
    assert any("protected path" in p for p in tool_problems("apply_patch", state, WORKSPACE))


def test_one_bad_path_in_a_many_file_patch_is_enough() -> None:
    state = _patch("*** Add File: fine.py", "+x", "*** Update File: also_fine.py", "@@", "-a", "+b", "*** Add File: .opencode/plugin/evil.js", "+x")
    assert len(tool_problems("apply_patch", state, WORKSPACE)) == 1


def test_case_cannot_hide_a_protected_path() -> None:
    assert tool_problems("write", _running(filePath=".OpenCode/x.js"), WORKSPACE)
    assert tool_problems("write", _running(filePath="OPENCODE.JSON"), WORKSPACE)


def test_reading_is_not_refused_for_what_only_writing_could_harm() -> None:
    assert tool_problems("read", _running(filePath="opencode.json"), WORKSPACE) == []


def test_a_denied_attempt_on_a_protected_path_still_counts() -> None:
    state = {"status": "error", "input": {"filePath": ".env"}, "error": "permission denied"}
    assert tool_problems("write", state, WORKSPACE)


def test_ordinary_env_example_files_stay_editable() -> None:
    assert tool_problems("write", _running(filePath=".env.example"), WORKSPACE) == []


# --- Leaving the project ------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.py", "sub/../../outside.py", "/workspace/../etc/passwd", "/workspace-other/a.py", "..", "/"])
@pytest.mark.parametrize("tool", ["read", "edit", "write"])
def test_a_path_outside_the_project_is_refused(tool: str, path: str) -> None:
    problems = tool_problems(tool, _running(filePath=path), WORKSPACE)
    assert len(problems) == 1 and "outside the project folder" in problems[0]


@pytest.mark.parametrize("tool", ["glob", "grep"])
def test_searches_stay_inside_too_but_may_start_at_the_top(tool: str) -> None:
    assert "outside the project folder" in tool_problems(tool, _running(path="/etc"), WORKSPACE)[0]
    assert tool_problems(tool, _running(path="/workspace"), WORKSPACE) == []
    assert tool_problems(tool, _running(path="."), WORKSPACE) == []


def test_a_patch_cannot_move_a_file_out_of_the_project() -> None:
    state = _patch("*** Update File: a.py", "*** Move to: ../../etc/cron.d/x", "@@", "-a", "+b")
    assert "outside the project folder" in tool_problems("apply_patch", state, WORKSPACE)[0]


@pytest.mark.parametrize(
    ("path", "expected"),
    [("a.py", "a.py"), ("/workspace/a.py", "a.py"), ("sub/../a.py", "a.py"), ("./a.py", "a.py"), (".", ""), ("/workspace", ""),
     ("/workspace/", ""), ("a\\b.py", "a/b.py"), ("../a.py", None), ("/etc/x", None), ("/workspace2/a", None)],
)  # fmt: skip
def test_paths_are_resolved_as_the_tools_resolve_them(path: str, expected: str | None) -> None:
    assert relative_path(path, WORKSPACE) == expected


# --- The real files have the last word ----------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "work"
    (folder / "src").mkdir(parents=True)
    (folder / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (folder / "README.md").write_text("# readme\n", encoding="utf-8")
    return folder


def _audit(root: Path, before: Any) -> Any:
    return audit(root, before, take_snapshot(root), max_file_bytes=1_000_000, max_files=100, secrets=[])


def test_the_audit_sees_a_create_edit_move_and_delete_for_what_they_did(root: Path) -> None:
    before = take_snapshot(root)
    (root / "src" / "new.py").write_text("x", encoding="utf-8")  # create
    (root / "README.md").write_text("changed", encoding="utf-8")  # edit
    (root / "src" / "main.py").rename(root / "src" / "moved.py")  # move
    report = _audit(root, before)
    assert report.clean
    assert {(c.path, c.kind) for c in report.changes} == {
        ("src/new.py", "added"), ("README.md", "modified"), ("src/main.py", "deleted"), ("src/moved.py", "added"),
    }  # fmt: skip


@pytest.mark.parametrize("destination", ["opencode.json", ".opencode/plugin/x.js", "sub/AGENTS.md", ".env"])
def test_the_audit_refuses_a_move_onto_a_protected_path_whatever_the_rules_allowed(root: Path, destination: str) -> None:
    before = take_snapshot(root)
    target = root / destination
    target.parent.mkdir(parents=True, exist_ok=True)
    (root / "src" / "main.py").rename(target)
    report = _audit(root, before)
    assert not report.clean and any(destination in v for v in report.violations)


def test_the_audit_refuses_deleting_a_protected_file(tmp_path: Path) -> None:
    folder = tmp_path / "w"
    folder.mkdir()
    (folder / "opencode.json").write_text("{}", encoding="utf-8")
    before = take_snapshot(folder)
    (folder / "opencode.json").unlink()
    assert not _audit(folder, before).clean


def test_a_patch_the_events_never_showed_is_still_caught_by_the_audit(root: Path) -> None:
    # The tripwire reads events, and an event can be missed. The audit reads the disk.
    before = take_snapshot(root)
    (root / ".opencode").mkdir()
    (root / ".opencode" / "plugin.js").write_text("evil", encoding="utf-8")
    assert file_operations("apply_patch", {"status": "running", "input": {}}) == []
    assert not _audit(root, before).clean
