"""The real-server contract check's pure parts: docker arguments, isolation and image checks,
the analysis of a probe report, the precedence analysis, and the probe's request allowlist.

No container, no server and no HTTP: every input is a plain value built here.
"""

from __future__ import annotations

import base64
import copy
import importlib.util
import io
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.coding.opencode_check import (
    BASE_IMAGE_ID,
    CHECK_USER,
    FAILED,
    GLOBAL_CONFIG_TARGET,
    GUARD_CONFIG_HOME,
    OPENCODE_GITIGNORE,
    HOSTILE_AGENT_NAME,
    HOSTILE_PROJECT_CONFIG,
    MEMORY_BYTES,
    NANO_CPUS,
    PASSWORD_ENV,
    PIDS_LIMIT,
    PRECEDENCE_MARKER,
    UNKNOWN,
    UNSUPPORTED,
    VERIFIED,
    CheckContainer,
    Check,
    analyze,
    analyze_precedence,
    apk_changes,
    build_exec_args,
    build_run_args,
    cleanup_check,
    compare_verdicts,
    hostile_workspace,
    image_problems,
    isolation_problems,
    isolation_summary,
    mount_problems,
    precedence_comparison,
    probe_input,
    prompt_fields,
    safe_console,
    write_report,
)
from app.coding.opencode_contract import PERMISSION_REPLY_OPERATIONS, REQUIRED_OPERATIONS, SENSITIVE_OPERATIONS
from app.coding.opencode_policy import (
    agent_rule_problems,
    config_problems,
    managed_config,
    session_rules,
    verify_effective_config,
)
from tests.helpers import OPENCODE_1_19_5_TOOLS, opencode_agents

PROBE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "opencode_check" / "probe.py"


def _container(root: Path, **kwargs: Any) -> CheckContainer:
    folders = {}
    for name in ("bin", "etc", "checks", "workspace"):
        folders[name] = root / "check" / name if name != "bin" else root / "cache" / "bin"
        folders[name].mkdir(parents=True, exist_ok=True)
    return CheckContainer(
        name="devpilot-opencode-check-test",
        binary_dir=folders["bin"],
        config_dir=folders["etc"],
        checks_dir=folders["checks"],
        workspace_dir=folders["workspace"],
        **kwargs,
    )


# --- docker arguments ------------------------------------------------------------------------


def test_the_run_arguments_carry_every_isolation_flag(tmp_path: Path) -> None:
    args = build_run_args(_container(tmp_path))
    joined = " ".join(args)
    for flag in ("--network none", "--cap-drop ALL", "--security-opt no-new-privileges", "--read-only",
                 f"--user {CHECK_USER}", "--memory 1g", "--memory-swap 1g", "--cpus 1", f"--pids-limit {PIDS_LIMIT}",
                 "--pull never", "--init", "--detach"):  # fmt: skip
        assert flag in joined, flag
    assert not {"-p", "--publish", "--publish-all", "--privileged", "--net", "-v", "--volume"} & set(args)
    assert "docker.sock" not in joined
    assert "/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777" in args
    assert args[-7:] == ["/opt/opencode/opencode", "serve", "--hostname", "127.0.0.1", "--port", "4096", "--print-logs"]


def test_the_password_is_passed_by_name_only(tmp_path: Path) -> None:
    args = build_run_args(_container(tmp_path))
    assert PASSWORD_ENV in args
    assert not any(a.startswith(PASSWORD_ENV + "=") for a in args)


def test_only_the_workspace_mount_is_writable(tmp_path: Path) -> None:
    mounts = [a for a in build_run_args(_container(tmp_path)) if a.startswith("type=bind")]
    assert len(mounts) == 4
    writable = [m for m in mounts if not m.endswith(",readonly")]
    assert len(writable) == 1 and writable[0].endswith("target=/workspace")


def test_project_config_flag_and_tmp_exec_follow_the_container_settings(tmp_path: Path) -> None:
    on = build_run_args(_container(tmp_path))
    off = build_run_args(_container(tmp_path, project_config_off=False, tmp_exec=True))
    assert "OPENCODE_DISABLE_PROJECT_CONFIG=1" in on and "OPENCODE_DISABLE_PROJECT_CONFIG=1" not in off
    assert any(a.startswith("/tmp:rw,exec,") for a in off)


def _guarded(root: Path) -> CheckContainer:
    c = _container(root)
    guard = root / "check" / "global-config"
    guard.mkdir()
    (guard / ".gitignore").write_text(OPENCODE_GITIGNORE)
    (c.workspace_dir / ".opencode").mkdir()
    return CheckContainer(**{**c.__dict__, "guard_config_dir": guard, "guard_project_opencode": True})


def test_the_install_guard_mounts_both_config_folders_read_only_and_moves_the_global_one(tmp_path: Path) -> None:
    args = build_run_args(_guarded(tmp_path))
    mounts = [a for a in args if a.startswith("type=bind")]
    assert len(mounts) == 6 and [m for m in mounts if not m.endswith(",readonly")] == [m for m in mounts if m.endswith("target=/workspace")]
    assert any(m.endswith(f"target={GLOBAL_CONFIG_TARGET},readonly") for m in mounts)
    assert any(m.endswith("target=/workspace/.opencode,readonly") for m in mounts)
    assert f"XDG_CONFIG_HOME={GUARD_CONFIG_HOME}" in args and "XDG_CONFIG_HOME=/tmp/home/.config" not in args
    assert "XDG_DATA_HOME=/tmp/home/.local/share" in args  # the rest of HOME stays in the tmpfs
    assert "XDG_CONFIG_HOME=/tmp/home/.config" in build_run_args(_container(tmp_path / "plain"))


def test_guard_mounts_are_checked(tmp_path: Path) -> None:
    c = _guarded(tmp_path)
    assert mount_problems(c, check_root=tmp_path / "check", binary_dir=c.binary_dir, forbidden=[]) == []
    (c.guard_config_dir / "plugin.js").write_text("x")  # type: ignore[operator]
    assert any("must hold only .gitignore" in p for p in mount_problems(c, check_root=tmp_path / "check", binary_dir=c.binary_dir, forbidden=[]))
    inspect = _inspect(c)
    assert isolation_problems(inspect, c) == []
    next(m for m in inspect["Mounts"] if m["Destination"] == "/workspace/.opencode")["RW"] = True
    assert "the mount at /workspace/.opencode is writable" in isolation_problems(inspect, c)


def test_the_probe_runs_as_a_separate_isolated_python_process() -> None:
    assert build_exec_args("c1", wait_seconds=60) == [
        "exec", "--user", CHECK_USER, "c1", "python3", "-I", "/checks/probe.py", "--wait", "60",
    ]  # fmt: skip


# --- mounts ----------------------------------------------------------------------------------


def test_fresh_folders_under_the_check_root_and_the_binary_folder_pass(tmp_path: Path) -> None:
    c = _container(tmp_path)
    assert mount_problems(c, check_root=tmp_path / "check", binary_dir=c.binary_dir, forbidden=[tmp_path / "repo"]) == []


def test_a_real_project_folder_or_another_binary_folder_is_refused(tmp_path: Path) -> None:
    c = _container(tmp_path)
    repo = tmp_path / "repo"
    (repo / "workspaces" / "p1").mkdir(parents=True)
    inside_repo = CheckContainer(**{**c.__dict__, "workspace_dir": repo / "workspaces" / "p1"})
    problems = mount_problems(inside_repo, check_root=tmp_path / "check", binary_dir=c.binary_dir, forbidden=[repo])
    assert any("overlaps a protected folder" in p for p in problems)
    assert any("fresh folder" in p for p in problems)

    other_bin = tmp_path / "check" / "other"
    other_bin.mkdir()
    swapped = CheckContainer(**{**c.__dict__, "binary_dir": other_bin})
    assert any("verified binary" in p for p in mount_problems(swapped, check_root=tmp_path / "check", binary_dir=c.binary_dir, forbidden=[]))


def test_a_mount_above_a_protected_folder_is_refused(tmp_path: Path) -> None:
    c = _container(tmp_path)
    problems = mount_problems(c, check_root=tmp_path / "check", binary_dir=c.binary_dir, forbidden=[tmp_path / "check" / "workspace" / "x"])
    assert any("overlaps" in p for p in problems)


def test_odd_missing_or_shared_mount_sources_are_refused(tmp_path: Path) -> None:
    c = _container(tmp_path)
    comma = tmp_path / "check" / "a,b"
    comma.mkdir()
    for change, reason in (
        ({"workspace_dir": comma}, "cannot be mounted safely"),
        ({"workspace_dir": tmp_path / "check" / "missing"}, "existing"),
        ({"workspace_dir": c.checks_dir}, "share a host folder"),
    ):
        problems = mount_problems(CheckContainer(**{**c.__dict__, **change}), check_root=tmp_path / "check",
                                  binary_dir=c.binary_dir, forbidden=[])  # fmt: skip
        assert any(reason in p for p in problems), (change, problems)


# --- docker inspect --------------------------------------------------------------------------


def _inspect(c: CheckContainer) -> dict[str, Any]:
    return {
        "Image": "sha256:abc",
        "HostConfig": {
            "NetworkMode": "none", "Privileged": False, "ReadonlyRootfs": True, "PublishAllPorts": False,
            "Memory": MEMORY_BYTES, "MemorySwap": MEMORY_BYTES, "NanoCpus": NANO_CPUS, "PidsLimit": PIDS_LIMIT,
            "CapDrop": ["ALL"], "CapAdd": None, "SecurityOpt": ["no-new-privileges"], "PortBindings": {},
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=256m,mode=1777"}, "Devices": [],
            "PidMode": "", "IpcMode": "private",
        },
        "Config": {
            "User": CHECK_USER,
            "Env": [f"{PASSWORD_ENV}=s3cret-value", "PATH=/usr/local/bin", "GPG_KEY=7169605F", *(f"{n}={v}" for n, v in c.environment())],
        },
        "Mounts": [
            {"Type": "bind", "Source": str(host), "Destination": target, "RW": not ro}
            for host, target, ro in c.mounts()
        ],
    }  # fmt: skip


def test_an_inspect_matching_the_requirements_passes(tmp_path: Path) -> None:
    c = _container(tmp_path)
    assert isolation_problems(_inspect(c), c) == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda i: i["HostConfig"].update(NetworkMode="bridge"), "NetworkMode"),
        (lambda i: i["HostConfig"].update(PortBindings={"4096/tcp": [{"HostPort": "4096"}]}), "port is published"),
        (lambda i: i["HostConfig"].update(Privileged=True), "Privileged"),
        (lambda i: i["HostConfig"].update(ReadonlyRootfs=False), "ReadonlyRootfs"),
        (lambda i: i["HostConfig"].update(CapDrop=[]), "capabilities"),
        (lambda i: i["HostConfig"].update(SecurityOpt=[]), "no-new-privileges"),
        (lambda i: i["HostConfig"].update(PidMode="host"), "namespace"),
        (lambda i: i["HostConfig"].update(Tmpfs={"/tmp": "rw,exec"}), "tmpfs"),
        (lambda i: i["HostConfig"].update(PidsLimit=None), "PidsLimit"),
        (lambda i: i["Config"].update(User="0:0"), "user"),
        (lambda i: i["Mounts"][0].update(RW=True), "writable"),
        (lambda i: i["Mounts"].append({"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"}), "Docker socket"),
        (lambda i: i["Mounts"].append({"Type": "volume", "Source": "x", "Destination": "/data"}), "unexpected mount"),
        (lambda i: i["Mounts"].pop(), "missing"),
        (lambda i: i["Config"]["Env"].append("OPENAI_API_KEY=sk-x"), "secret-like"),
        (lambda i: i["Config"].update(Env=["PATH=/bin"]), "password variable is missing"),
        (lambda i: i["Config"]["Env"].append("OPENCODE_CONFIG=/workspace/x.json"), "unexpected environment variables"),
        (lambda i: i["Config"]["Env"].remove("OPENCODE_DISABLE_PROJECT_CONFIG=1"), "OPENCODE_DISABLE_PROJECT_CONFIG is missing"),
    ],
)
def test_each_isolation_deviation_is_reported(tmp_path: Path, change: Any, reason: str) -> None:
    c = _container(tmp_path)
    inspect = _inspect(c)
    change(inspect)
    assert any(reason in p for p in isolation_problems(inspect, c)), isolation_problems(inspect, c)


def test_the_saved_summary_has_variable_names_but_no_values(tmp_path: Path) -> None:
    summary = isolation_summary(_inspect(_container(tmp_path)))
    assert "s3cret-value" not in json.dumps(summary)
    assert PASSWORD_ENV in summary["env_names"]


def test_the_check_image_must_be_the_pinned_base_plus_one_layer() -> None:
    base = {"Id": BASE_IMAGE_ID, "RootFS": {"Layers": ["l1", "l2"]}}
    good = {"RootFS": {"Layers": ["l1", "l2", "libs"]}, "Config": {"User": CHECK_USER}, "Os": "linux", "Architecture": "amd64"}
    assert image_problems(good, base) == []
    assert image_problems({**good, "RootFS": {"Layers": ["l1", "l2", "a", "b"]}}, base)
    assert image_problems({**good, "RootFS": {"Layers": ["x1", "l2", "libs"]}}, base)
    assert image_problems(good, {**base, "Id": "sha256:other"})
    assert image_problems({**good, "Config": {"User": "root"}}, base)


APK_OK = """#5 [2/2] RUN apk add --no-cache --no-scripts libstdc++ libgcc
#5 0.31 fetch https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/APKINDEX.tar.gz
#5 1.02 (1/2) Installing libgcc (15.2.0-r3)
#5 1.05 (2/2) Installing libstdc++ (15.2.0-r3)
#5 1.20 OK: 48 MiB in 40 packages
"""


def test_the_build_may_install_exactly_the_two_libraries() -> None:
    installed, problems = apk_changes(APK_OK)
    assert installed == {"libgcc": "15.2.0-r3", "libstdc++": "15.2.0-r3"}
    assert problems == []


def test_an_extra_package_an_upgrade_or_a_missing_library_is_refused() -> None:
    extra = APK_OK.replace("(2/2) Installing libstdc++", "(2/3) Installing libstdc++") + "#5 1.1 (3/3) Installing curl (8.0-r0)\n"
    assert any("unexpected package: curl" in p for p in apk_changes(extra)[1])
    upgrade = APK_OK + "#5 1.1 (3/3) Upgrading musl (1.2.5-r10 -> 1.2.5-r11)\n"
    assert any("upgrading musl" in p for p in apk_changes(upgrade)[1])
    assert any("libstdc++" in p for p in apk_changes(APK_OK.replace("(2/2) Installing libstdc++ (15.2.0-r3)", ""))[1])
    assert apk_changes("")[1] == ["apk did not report installing libgcc, libstdc++"]


def test_cleanup_passes_only_when_nothing_is_left() -> None:
    assert cleanup_check({"removed": True, "left": [], "run_folder_removed": True}).verdict == "PASS"
    assert cleanup_check({"removed": True, "left": ["abc"], "run_folder_removed": True}).verdict == "FAIL"
    assert cleanup_check({"removed": False, "left": [], "run_folder_removed": True}).verdict == "FAIL"
    assert cleanup_check({}).verdict == "FAIL"


def test_statuses_map_to_pass_fail_and_not_verified() -> None:
    from app.coding.opencode_check import Check

    verdicts = {s: Check("X", "t", s, "d").verdict for s in (VERIFIED, FAILED, UNSUPPORTED, UNKNOWN)}
    assert verdicts == {VERIFIED: "PASS", FAILED: "FAIL", UNSUPPORTED: "FAIL", UNKNOWN: "NOT VERIFIED"}


# --- analysis of a probe report --------------------------------------------------------------


def _path(template: str) -> str:
    names = iter(("sessionID", "permissionID", "x"))
    return re.sub(r"\{\}", lambda _: "{" + next(names) + "}", template)


def _spec() -> dict[str, Any]:
    paths: dict[str, dict[str, Any]] = {}
    for method, template in [*REQUIRED_OPERATIONS, PERMISSION_REPLY_OPERATIONS[0], *SENSITIVE_OPERATIONS]:
        paths.setdefault(_path(template), {})[method] = {"responses": {}}
    return {"openapi": "3.1.1", "paths": paths}


def _ok(body: Any, status: int = 200, content_type: str = "application/json") -> dict[str, Any]:
    return {"status": status, "headers": {"content-type": content_type}, "json": body, "bytes": 10}


def _refused() -> dict[str, Any]:
    return {"status": 401, "headers": {"www-authenticate": "Basic"}, "text": "Unauthorized"}


def _good_report() -> dict[str, Any]:
    sse = b'data: {"type":"server.connected","properties":{}}\n\n'
    return {
        "startup": {"ready": True, "seconds": 2.5},
        "health": _ok({"healthy": True, "version": "1.19.5"}),
        "auth": {
            "health_without_password": _refused(),
            "health_with_wrong_password": _refused(),
            "doc_without_password": _refused(),
            "session_create_without_password": _refused(),
            "event_without_password": _refused(),
        },
        "doc": _ok(_spec()),
        "config": _ok(_managed()),
        "agents": _ok(opencode_agents(_managed())),
        "providers": _ok(_providers()),
        "tool_ids": _ok(list(OPENCODE_1_19_5_TOOLS)),
        "ripgrep": {"on_path": "/usr/bin/rg", "cached": False},
        "session_create_with_rules": _ok({"id": "ses_abc", "permission": session_rules()}),
        "session_get": _ok({"id": "ses_abc"}),
        "session_status": _ok({}),
        "session_messages": _ok([]),
        "session_abort": _ok(True),
        "session_status_after_abort": _ok({}),
        "session_unknown": {"status": 404, "headers": {}, "text": "Not Found"},
        "event": {"status": 200, "headers": {"content-type": "text/event-stream"}, "raw_b64": base64.b64encode(sse).decode(), "seconds": 0.1},
        "unknown_path": {"status": 404, "headers": {}, "text": "Not Found"},
        "filesystem": _filesystem(),
    }  # fmt: skip


def _providers(package: str = "@ai-sdk/openai", provider: str = "openai") -> dict[str, Any]:
    model = {"id": "m", "providerID": provider, "api": {"id": "m", "url": "http://x", "npm": package}}
    return {"all": [{"id": provider, "name": provider, "models": {"m": model}}], "default": {}, "connected": []}


def _filesystem(*extra: str) -> dict[str, Any]:
    home = [{"path": p, "type": "file", "size": 1} for p in (".local/share/opencode/log/run.log", *extra)]
    return {
        "/tmp/home": {"exists": True, "entries": home, "truncated": False},
        "/workspace": {"exists": True, "entries": [], "truncated": False},
        "/opt/devpilot/xdg-config": {"exists": True, "entries": [{"path": "opencode/.gitignore", "type": "file", "size": 50}], "truncated": False},
    }


def _by_id(checks: list[Any]) -> dict[str, Any]:
    return {c.id: c for c in checks}


def _analyze(report: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"isolation": [], "workspace_changes": [], "expected_version": "1.19.5"} | kwargs
    return _by_id(analyze(report, **args))


def test_a_server_matching_every_assumption_is_verified_throughout() -> None:
    checks = _analyze(_good_report())
    assert {k: c.status for k, c in checks.items() if c.status != VERIFIED} == {}
    assert "echoed exactly" in checks["C6a"].detail
    assert "server.connected" in checks["C7"].detail
    assert "/permission/{}/reply" in checks["C11"].detail
    assert "POST /session/{}/shell" in checks["C10"].detail


def test_the_prompt_body_fields_are_read_from_the_spec_through_a_ref() -> None:
    spec = _spec()
    spec["paths"]["/session/{sessionID}/prompt_async"]["post"] = {
        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/PromptInput"}}}}
    }
    spec["components"] = {"schemas": {"PromptInput": {"properties": {"parts": {}, "messageID": {}, "agent": {}}}}}
    assert prompt_fields(spec) == ["agent", "messageID", "parts"]
    report = _good_report()
    report["doc"] = _ok(spec)
    assert "client messageID accepted: yes" in _analyze(report)["C11"].detail
    assert prompt_fields(_spec()) is None
    assert "client messageID accepted: unknown" in _analyze(_good_report())["C11"].detail


def test_a_server_that_never_starts_leaves_everything_else_unknown() -> None:
    checks = _analyze({"startup": {"ready": False, "seconds": 60, "last": "ConnectionRefusedError"}})
    assert checks["C1"].status == FAILED and "ConnectionRefusedError" in checks["C1"].detail
    assert all(c.status == UNKNOWN for k, c in checks.items() if k not in ("C0", "C1", "C14"))


def test_an_html_doc_is_unsupported_and_the_spec_checks_become_unknown() -> None:
    report = _good_report()
    report["doc"] = {"status": 200, "headers": {"content-type": "text/html; charset=utf-8"},
                     "text": "<!doctype html><script src='/doc/spec.json'>", "html_refs": ["/doc/spec.json"]}  # fmt: skip
    checks = _analyze(report)
    assert checks["C5"].status == UNSUPPORTED and "/doc/spec.json" in checks["C5"].detail
    assert checks["C10"].status == UNKNOWN and checks["C11"].status == UNKNOWN


def test_any_answer_without_a_valid_password_fails_authentication() -> None:
    report = _good_report()
    report["auth"]["session_create_without_password"] = _ok({"id": "ses_x"})
    assert _analyze(report)["C3"].status == FAILED
    report = _good_report()
    report["auth"]["event_without_password"] = {"error": "TimeoutError: timed out"}
    assert _analyze(report)["C3"].status == UNKNOWN


def test_a_wrong_version_fails() -> None:
    report = _good_report()
    report["health"] = _ok({"healthy": True, "version": "1.19.4"})
    assert _analyze(report)["C2"].status == FAILED
    report["health"] = _ok({"healthy": True, "version": "2.2.2"})
    assert "not supported" in _analyze(report)["C2"].detail


def test_the_servers_reordered_config_passes_c8_and_its_enforced_rules_pass_c9() -> None:
    # Regression for the Phase 4C C8 failure, with the key order the real server returned.
    config = _managed()
    order = ["read", "edit", "glob", "grep", "list", "bash", "task", "external_directory", "todowrite", "question",
             "webfetch", "websearch", "lsp", "doom_loop", "skill", "*", "codesearch"]  # fmt: skip
    config["permission"] = {key: config["permission"][key] for key in order}
    report = _good_report()
    report["config"] = _ok(config)
    checks = _analyze(report)
    assert checks["C8"].status == VERIFIED and "not judged here" in checks["C8"].detail
    assert checks["C9"].status == VERIFIED and "101 ordered rules" in checks["C9"].detail


def test_a_weakened_declaration_fails_c8_and_a_weakened_enforced_rule_fails_c9() -> None:
    report = _good_report()
    config = _managed()
    config["permission"]["bash"] = "allow"
    report["config"] = _ok(config)
    report["agents"] = _ok(opencode_agents(_managed(), extra_rules=[{"permission": "bash", "pattern": "*", "action": "allow"}]))
    checks = _analyze(report)
    assert checks["C8"].status == FAILED and "'bash' differs" in checks["C8"].detail
    assert checks["C9"].status == FAILED and "bash is not denied" in checks["C9"].detail


def test_a_listed_subagent_or_a_missing_or_malformed_agent_list_fails_c9() -> None:
    report = _good_report()
    report["agents"] = _ok([*opencode_agents(_managed()), {"name": "general", "permission": []}])
    assert "general" in _analyze(report)["C9"].detail
    for broken in (_ok([a for a in opencode_agents(_managed()) if a["name"] != "build"]), _ok({"build": {}}),
                   {"status": 500, "headers": {}, "text": "boom"}):  # fmt: skip
        report["agents"] = broken
        assert _analyze(report)["C9"].status == FAILED


def test_session_rule_refusal_is_unsupported_and_odd_statuses_are_unknown() -> None:
    report = _good_report()
    report["session_create_with_rules"] = {"status": 400, "headers": {}, "json": {"error": "bad"}}
    report["session_create_plain"] = _ok({"id": "ses_abc"})
    report["session_unknown"] = {"status": 500, "headers": {}, "text": "boom"}
    checks = _analyze(report)
    assert checks["C6a"].status == UNSUPPORTED
    assert checks["C6b"].status == VERIFIED
    assert checks["C6f"].status == UNKNOWN


def test_no_session_at_all_fails_and_marks_the_rest_unknown() -> None:
    report = _good_report()
    report["session_create_with_rules"] = {"status": 500, "headers": {}, "text": "x"}
    report["session_create_plain"] = {"status": 500, "headers": {}, "text": "x"}
    checks = _analyze(report)
    assert checks["C6a"].status == FAILED
    assert {checks[k].status for k in ("C6b", "C6c", "C6d", "C6e")} == {UNKNOWN}


def test_the_reviewed_1_19_5_tool_inventory_is_verified_against_the_enforced_rules() -> None:
    check = _analyze(_good_report())["C12"]
    assert check.status == VERIFIED
    assert "Offered and allowed: apply_patch, edit, glob, grep, invalid, read, todowrite, write" in check.detail
    assert "Denied and hidden from the model: bash, question, skill, task, webfetch, websearch" in check.detail


def test_tools_outside_the_reviewed_set_stay_not_verified() -> None:
    report = _good_report()
    report["tool_ids"] = _ok([*OPENCODE_1_19_5_TOOLS, "lsp_hover", "pty_exec"])
    check = _analyze(report)["C12"]
    assert check.status == UNKNOWN and "lsp_hover" in check.detail and "pty_exec" in check.detail


def test_a_tool_the_rules_treat_differently_from_the_review_fails() -> None:
    report = _good_report()
    report["agents"] = _ok(opencode_agents(_managed(), extra_rules=[{"permission": "edit", "pattern": "*", "action": "deny"},
                                                                   {"permission": "bash", "pattern": "ls", "action": "allow"}]))  # fmt: skip
    check = _analyze(report)["C12"]
    assert check.status == FAILED
    assert "apply_patch (edit) should be offered and allowed" in check.detail
    assert "bash (bash) should be denied and hidden from the model" in check.detail


def test_the_tool_inventory_needs_the_enforced_rules() -> None:
    report = _good_report()
    report["agents"] = {"status": 500, "headers": {}, "text": "boom"}
    assert _analyze(report)["C12"].status == UNKNOWN


def test_unknown_paths_that_are_served_and_workspace_writes_are_reported() -> None:
    report = _good_report()
    report["unknown_path"] = {"status": 200, "headers": {"content-type": "text/html"}, "text": "<html>"}
    checks = _analyze(report, workspace_changes=["+.opencode/package.json"], isolation=["a port is published"])
    assert checks["C13"].status == UNKNOWN and "API filter" in checks["C13"].detail
    assert checks["C14"].status == FAILED and ".opencode/package.json" in checks["C14"].detail
    assert checks["C0"].status == FAILED


def test_install_artifacts_anywhere_in_the_listing_fail_c16() -> None:
    assert _analyze(_good_report())["C16"].status == VERIFIED
    report = _good_report()
    report["filesystem"] = _filesystem(".config/opencode/node_modules/@opencode-ai/plugin/index.js", ".config/opencode/package.json")
    check = _analyze(report)["C16"]
    assert check.status == FAILED and "node_modules" in check.detail and "package.json" in check.detail


def test_a_missing_or_truncated_listing_leaves_c16_not_verified() -> None:
    report = _good_report()
    del report["filesystem"]
    assert _analyze(report)["C16"].status == UNKNOWN
    report["filesystem"] = _filesystem()
    report["filesystem"]["/tmp/home"]["truncated"] = True
    assert _analyze(report)["C16"].status == UNKNOWN


def test_c16_says_it_is_an_observation_and_not_a_proof() -> None:
    check = _analyze(_good_report())["C16"]
    assert check.verdict == "PASS" and "This is what was observed" in check.detail and "prevents an attempt" in check.detail


@pytest.mark.parametrize("line", ["background dependency install failed", "downloading ripgrep"])
def test_a_logged_attempt_fails_c16_even_with_nothing_on_disk(line: str) -> None:
    check = _analyze(_good_report(), server_log=f"timestamp=x level=WARN message={line}\n")["C16"]
    assert check.status == FAILED and "install attempt" in check.detail


def test_a_home_config_folder_fails_c16() -> None:
    report = _good_report()
    report["filesystem"] = _filesystem(".opencode/agent/x.md")
    check = _analyze(report)["C16"]
    assert check.status == FAILED and "/tmp/home/.opencode" in check.detail


def test_only_bundled_provider_packages_pass_c17() -> None:
    check = _analyze(_good_report())["C17"]
    assert check.status == VERIFIED and "@ai-sdk/openai" in check.detail and "no model was called" in check.detail
    report = _good_report()
    report["providers"] = _ok(_providers(package="some-npm-provider"))
    check = _analyze(report)["C17"]
    assert check.status == FAILED and "some-npm-provider" in check.detail
    report["providers"] = _ok(_providers(provider="anthropic"))
    check = _analyze(report)["C17"]
    assert check.status == FAILED and "anthropic" in check.detail


def test_a_provider_list_that_says_nothing_leaves_c17_not_verified() -> None:
    report = _good_report()
    for broken in ({"status": 500, "headers": {}, "text": "boom"}, _ok({"all": []}), _ok({"all": "x"}), None):
        report["providers"] = broken
        assert _analyze(report)["C17"].status == UNKNOWN


def test_ripgrep_present_passes_c18_and_absent_stays_not_verified() -> None:
    assert _analyze(_good_report())["C18"].status == VERIFIED
    report = _good_report()
    report["ripgrep"] = {"on_path": None, "cached": True}
    assert _analyze(report)["C18"].status == VERIFIED
    report["ripgrep"] = {"on_path": None, "cached": False}
    check = _analyze(report)["C18"]
    assert check.verdict == "NOT VERIFIED"
    assert "the artifact is prevented and the attempt is not" in check.detail
    del report["ripgrep"]
    assert _analyze(report)["C18"].status == UNKNOWN


def test_a_logged_ripgrep_download_fails_c18() -> None:
    assert _analyze(_good_report(), server_log="message=downloading ripgrep url=x")["C18"].status == FAILED


def test_the_baseline_always_runs_with_the_install_guard(runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(runner, "_preflight", lambda **_: (tmp_path, "sha"))
    monkeypatch.setattr(runner, "_sweep", lambda: [])
    monkeypatch.setattr(runner, "one_run", lambda *a, **k: calls.append({"args": a, **k}) or {})
    monkeypatch.setattr(runner, "finish_baseline", lambda *a, **k: {"passed": True})
    assert runner.run("baseline", tmp_exec=False) == 0
    assert calls[0]["guard"] is True and calls[0]["project_config_off"] is True


def test_an_event_stream_without_server_connected_is_unknown() -> None:
    report = _good_report()
    report["event"]["raw_b64"] = base64.b64encode(b": heartbeat\n\n").decode()
    assert _analyze(report)["C7"].status == UNKNOWN
    report["event"]["headers"] = {"content-type": "application/json"}
    assert _analyze(report)["C7"].status == FAILED


# --- configuration precedence ------------------------------------------------------------------


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Deep merge as JavaScript object spread does it: a key keeps the position where it first appeared."""
    out = copy.deepcopy(base)
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(out.get(key), dict) and isinstance(value, dict) else copy.deepcopy(value)
    return out


def _managed() -> dict[str, Any]:
    return managed_config(model="openai/x", gateway_base_url="http://gw.invalid/v1")


def test_the_hostile_workspace_is_inert_text() -> None:
    files = hostile_workspace()
    project = json.loads(files["opencode.json"])
    assert project == HOSTILE_PROJECT_CONFIG
    text = json.dumps(project) + "".join(files.values())
    for key in ('"mcp"', '"plugin"', '"command"', '"formatter"', '"lsp"', '"instructions"', '"provider"', "apiKey", "http://"):
        assert key not in text
    assert all(path.endswith((".json", ".md", ".gitignore")) for path in files)
    assert files[".opencode/.gitignore"] == OPENCODE_GITIGNORE  # what OpenCode would write; it then writes nothing there


def _run_record(config: dict[str, Any], agents: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"report": {"config": _ok(config), "agents": _ok(agents), "filesystem": _filesystem()},
            "isolation_problems": [], "workspace_changes": [], "cleanup": {"removed": True, "left": [], "run_folder_removed": True},
            **extra}  # fmt: skip


def test_the_comparison_shows_declared_enforced_and_devpilots_verdict(runner: ModuleType) -> None:
    merged = _merge(HOSTILE_PROJECT_CONFIG, _managed())
    clean = _managed()
    comparison = precedence_comparison(_run_record(merged, opencode_agents(merged)), _run_record(clean, opencode_agents(clean)))
    on, off = comparison["project_config_enabled"], comparison["project_config_disabled"]
    assert on["project_file_loaded"] and not off["project_file_loaded"]
    assert on["enforced_decisions"]["bash 'ls'"] == "allow" and on["enforced_decisions"]["bash offered to the model"]
    assert off["enforced_decisions"]["bash 'ls'"] == "deny" and not off["enforced_decisions"]["bash offered to the model"]
    assert on["wildcard_permission_rules"] and off["wildcard_permission_rules"] == []
    assert on["devpilot_verdict"] == "rejected" and off["devpilot_verdict"] == "accepted"
    assert on["install_check"]["verdict"] == "PASS"
    summary = {"binary_sha256": "ab", "image_id": "sha256:x", "comparison": comparison, "precedence_checks": []}
    markdown = runner._precedence_markdown(summary)
    assert "| DevPilot verdict | rejected | accepted |" in markdown
    assert "| Enforced: bash 'ls' | allow | deny |" in markdown


def test_a_project_file_naming_keys_first_reorders_the_enforced_rules_and_the_checks_catch_it() -> None:
    # Why the precedence run matters: if OpenCode merges as JavaScript spreads objects, the project
    # file fixes the order, and last-match-wins then lets its later wildcard rules beat managed denials.
    merged = _merge(HOSTILE_PROJECT_CONFIG, _managed())
    assert merged["permission"]["bash"] == "deny"  # every managed value still wins its own key ...
    enforced = agent_rule_problems(opencode_agents(merged))
    assert any("bash is not denied" in p for p in enforced)  # ... yet bash is allowed through "b*sh"
    assert any("wildcard permission rule ('b*sh'" in p for p in enforced)
    assert "global: the declared rules hold keys DevPilot did not set: b*sh" in config_problems(merged)
    clean = _merge({}, _managed())
    assert verify_effective_config(clean, opencode_agents(clean)) == []


def _precedence(config: dict[str, Any], agents: list[dict[str, Any]]) -> dict[str, Any]:
    return {"config": _ok(config), "agents": _ok(agents)}


def test_precedence_reports_a_weakening_in_both_views() -> None:
    merged = _merge(HOSTILE_PROJECT_CONFIG, _managed())
    clean = _managed()
    checks = _by_id(analyze_precedence(_precedence(merged, opencode_agents(merged)), _precedence(clean, opencode_agents(clean))))
    assert checks["P1"].status == VERIFIED
    assert checks["P2"].status == FAILED and "bash is not denied" in checks["P2"].detail
    assert checks["P3"].status == FAILED and "b*sh" in checks["P3"].detail
    assert checks["P4"].status == VERIFIED
    assert checks["P5"].status == VERIFIED


def test_precedence_flags_an_added_agent_and_a_flag_that_is_not_honoured() -> None:
    clean_with_marker = {**_managed(), "username": PRECEDENCE_MARKER}
    agents = [*opencode_agents(_managed()), {"name": HOSTILE_AGENT_NAME, "permission": []}]
    checks = _by_id(analyze_precedence(_precedence(clean_with_marker, agents), _precedence(clean_with_marker, agents)))
    assert checks["P2"].status == FAILED and HOSTILE_AGENT_NAME in checks["P2"].detail
    assert checks["P3"].status == VERIFIED  # the declarations alone look fine
    assert checks["P4"].status == FAILED
    assert checks["P5"].status == UNSUPPORTED  # the marker survived the flag


def test_precedence_without_the_project_file_loaded_is_not_tested() -> None:
    clean = _managed()
    checks = _by_id(analyze_precedence(_precedence(clean, opencode_agents(clean)), _precedence(clean, opencode_agents(clean))))
    assert checks["P1"].status == UNKNOWN
    assert checks["P2"].status == UNKNOWN and checks["P3"].status == UNKNOWN
    assert checks["P5"].status == VERIFIED


def test_the_probe_input_is_only_the_session_rules() -> None:
    assert probe_input() == {"session_rules": session_rules()}


# --- the probe's allowlist (the script is imported, never run) ---------------------------------


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("devpilot_opencode_probe", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_probe_allows_no_prompt_command_or_configuration_change(probe: ModuleType) -> None:
    allowed = probe.ALLOWED
    posts = {path for method, path in allowed if method != "GET"}
    assert posts == {"/session", "/session/{id}/abort"}
    for path in ("/session/{id}/prompt_async", "/session/{id}/message", "/session/{id}/shell", "/session/{id}/command"):
        assert ("POST", path) not in allowed
    normalized = {(m.lower(), re.sub(r"\{[^}]+\}", "{}", p)) for m, p in allowed}
    assert not normalized & SENSITIVE_OPERATIONS
    risky = [p for _, p in allowed if p.startswith(("/mcp", "/tui", "/pty", "/instance", "/auth", "/config/"))]
    assert risky == []
    assert [p for _, p in allowed if p.startswith("/experimental")] == ["/experimental/tool/ids"]  # one read-only GET
    assert [(m, p) for m, p in allowed if "provider" in p] == [("GET", "/provider")]  # no provider auth or OAuth call


def test_the_probe_refuses_anything_outside_its_allowlist(probe: ModuleType) -> None:
    for method, path in (("PATCH", "/config"), ("POST", "/session/{id}/shell"), ("POST", "/session/{id}/prompt_async"),
                         ("PUT", "/auth/{id}"), ("GET", "/session/{id}/diff"), ("POST", "/mcp")):  # fmt: skip
        with pytest.raises(probe.Refused):
            probe.target(method, path, {"id": "ses_1"})


def test_the_probe_refuses_ids_that_could_change_the_path(probe: ModuleType) -> None:
    assert probe.target("GET", "/session/{id}", {"id": "ses_ABC-1_x"}, {"directory": "/workspace"}) == (
        "/session/ses_ABC-1_x?directory=%2Fworkspace"
    )
    for bad in ("../config", "ses/../../x", "ses?x=1", "", "a" * 200):
        with pytest.raises(probe.Refused):
            probe.target("GET", "/session/{id}", {"id": bad})
    with pytest.raises(probe.Refused):
        probe.target("GET", "/session/{id}", {})


def test_the_probe_lists_names_and_sizes_without_reading_or_following(probe: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("secret contents")
    (tmp_path / ".gitignore").write_text("node_modules\n")
    out = probe.listing(str(tmp_path))
    assert out["exists"] and not out["truncated"]
    assert {e["path"]: e["type"] for e in out["entries"]} == {
        "node_modules": "dir", ".gitignore": "file", "node_modules/pkg": "dir", "node_modules/pkg/index.js": "file",
    }  # fmt: skip
    assert "secret contents" not in json.dumps(out)
    assert probe.listing(str(tmp_path / "missing")) == {"exists": False, "entries": [], "truncated": False}


def test_the_probe_records_html_links_without_following_them(probe: ModuleType) -> None:
    html = b"<!doctype html><script src=\"/assets/app.js\"></script><a href='/doc/json'>x</a>"
    out = probe.describe(html)
    assert out["html_refs"] == ["/assets/app.js", "/doc/json"]
    assert "json" not in out
    assert probe.describe(b'{"a": 1}')["json"] == {"a": 1}


def test_the_probe_never_puts_the_real_password_in_a_wrong_password_header(probe: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "the-real-one")
    assert probe.auth_header("none") == {}
    wrong = base64.b64decode(probe.auth_header("wrong")["Authorization"].split()[1]).decode()
    good = base64.b64decode(probe.auth_header("good")["Authorization"].split()[1]).decode()
    assert wrong == "opencode:devpilot-wrong-password"
    assert good == "opencode:the-real-one"


# --- reports (the first Phase 4C baseline lost its evidence to the console) ---------------------


RUNNER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "opencode_contract_check.py"
UNICODE_ERROR = "docker run failed: café → ✓"  # é exists in cp1252; → and ✓ do not


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("devpilot_contract_check_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cp1252_console() -> io.TextIOWrapper:
    """A restricted Windows console: cp1252 with strict errors, as the PowerShell pipe was."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


def _failed_start() -> dict[str, Any]:
    return {"stamp": "20990101T000000Z", "error": UNICODE_ERROR,
            "cleanup": {"removed": True, "left": [], "run_folder_removed": True}}  # fmt: skip


def test_evidence_is_saved_before_a_restricted_console_can_fail(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "REPORTS", tmp_path)
    with pytest.raises(UnicodeEncodeError):  # the unguarded console still fails ...
        runner.finish_baseline(_failed_start(), "ab" * 32, stream=_cp1252_console())
    saved = json.loads((tmp_path / "20990101T000000Z-baseline.json").read_text(encoding="utf-8"))
    summary = json.loads((tmp_path / "20990101T000000Z-baseline-summary.json").read_text(encoding="utf-8"))
    assert saved["error"] == UNICODE_ERROR  # ... but only after both files were written, intact
    assert summary["passed"] is False and summary["checks"][0]["detail"] == UNICODE_ERROR


def test_a_guarded_restricted_console_prints_replacements_and_the_comparison(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "REPORTS", tmp_path)
    write_report(tmp_path / "latest-baseline-summary.json", {"report_file": "old.json", "checks": [{"id": "C1", "verdict": "PASS"}]})
    console = _cp1252_console()
    safe_console(console)
    summary = runner.finish_baseline(_failed_start(), "ab" * 32, stream=console)
    console.flush()
    text = console.buffer.getvalue().decode("cp1252")
    assert "café ? ?" in text
    assert "C1: PASS -> FAIL" in text and summary["changes_since_previous"][0] == "C1: PASS -> FAIL"
    assert summary["previous_report_file"] == "old.json"
    latest = json.loads((tmp_path / "latest-baseline-summary.json").read_text(encoding="utf-8"))
    assert latest["checks"][0]["detail"] == UNICODE_ERROR


def test_an_analysis_error_is_saved_with_a_bounded_reason(runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "REPORTS", tmp_path)

    def broken(record: dict[str, Any]) -> list[Any]:
        raise RuntimeError("x" * 5000)

    monkeypatch.setattr(runner, "_analysis", broken)
    console = _cp1252_console()
    summary = runner.finish_baseline(_failed_start(), "ab" * 32, stream=console)
    assert summary["passed"] is False and len(summary["analysis_error"]) == 2000
    assert (tmp_path / "20990101T000000Z-baseline.json").exists()


def test_reports_are_utf8_and_replaced_atomically(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    write_report(path, {"detail": "health → 401", "n": 1})
    assert "→".encode() in path.read_bytes()  # real UTF-8, not an escape
    circular: list[Any] = []
    circular.append(circular)
    with pytest.raises(ValueError):
        write_report(path, {"n": 2, "loop": circular})
    assert json.loads(path.read_text(encoding="utf-8")) == {"detail": "health → 401", "n": 1}  # earlier file intact
    assert [p.name for p in tmp_path.iterdir()] == ["report.json"]  # no temporary file left


def test_verdict_changes_against_the_previous_summary_are_listed() -> None:
    previous = {"checks": [{"id": "C1", "verdict": "PASS"}, {"id": "C8", "verdict": "FAIL"},
                           {"id": "C12", "verdict": "NOT VERIFIED"}, {"id": "C99", "verdict": "PASS"}]}  # fmt: skip
    current = [Check(i, "t", VERIFIED, "d") for i in ("C1", "C8", "C12", "C15")]
    assert compare_verdicts(previous, current) == ["C8: FAIL -> PASS", "C12: NOT VERIFIED -> PASS", "C15: new, PASS", "C99: PASS -> not reported"]
