"""Phase 4C real-server contract check for the official OpenCode 1.19.5 binary.

Default: a DRY RUN. It verifies the kept binary against its manifest, checks which images are
present, and prints the exact docker commands, environment, managed configuration and
precedence files. It starts nothing and downloads nothing.

    & $env:DEVPILOT_PYTHON -I backend\\scripts\\opencode_contract_check.py                 # dry run
    & $env:DEVPILOT_PYTHON -I backend\\scripts\\opencode_contract_check.py --build-image   # APPROVAL: apk downloads 2 packages
    & $env:DEVPILOT_PYTHON -I backend\\scripts\\opencode_contract_check.py --run baseline  # APPROVAL: starts the server
    & $env:DEVPILOT_PYTHON -I backend\\scripts\\opencode_contract_check.py --run precedence  # APPROVAL, after a passing baseline
    & $env:DEVPILOT_PYTHON -I backend\\scripts\\opencode_contract_check.py --cleanup       # removes leftovers of this check only

A run: one container per server start, no network, no published port, read-only root, no
capabilities, user 65534, 1 GB memory, 1 CPU, 256 PIDs, a 256 MB tmpfs, and five mounts (the
verified binary, DevPilot's managed config, the install guard, the probe, a fresh empty workspace;
the precedence runs add the workspace's .opencode/ folder, read-only). A fresh random
password is generated per start and passed to docker by name only. The probe runs inside the
container through `docker exec` against 127.0.0.1:4096; nothing is exposed to the host. The
container and the run folder are removed on every path; the redacted report is kept.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from app.coding.opencode_artifact import ArtifactError, verify_binary
from app.coding.opencode_check import (
    BASE_IMAGE,
    CHECK_IMAGE,
    CHECK_IMAGE_ID,
    CHECK_LABEL,
    FAILED,
    PASSWORD_ENV,
    PROJECT_CONFIG_OFF,
    SERVER_ENV,
    Check,
    CheckContainer,
    analyze,
    analyze_precedence,
    apk_changes,
    build_exec_args,
    build_run_args,
    cleanup_check,
    compare_verdicts,
    hostile_workspace,
    image_problems,
    OPENCODE_GITIGNORE,
    isolation_problems,
    isolation_summary,
    mount_problems,
    precedence_comparison,
    probe_input,
    safe_console,
    write_report,
)
from app.coding.opencode_contract import CONTRACT_VERSION
from app.coding.opencode_policy import managed_config
from app.config import REPO_ROOT

CACHE = Path(r"D:\DevPilotCache")  # on D: (CLAUDE.md: nothing large on C:)
MANIFEST = CACHE / "opencode" / CONTRACT_VERSION / "manifest.json"
CHECK_ROOT = CACHE / "opencode-check"
RUNS = CHECK_ROOT / "runs"
REPORTS = CHECK_ROOT / "reports"
HERE = Path(__file__).resolve().parent
PROBE = HERE / "opencode_check" / "probe.py"
RELAY = HERE / "opencode_check" / "http_relay.py"
IMAGE_CONTEXT = HERE / "opencode_check" / "image"
# Placeholders: no prompt is ever sent, and the container has no network to reach anything.
CHECK_MODEL = "openai/devpilot-contract-check"
CHECK_GATEWAY = "http://devpilot-model-gateway.invalid/v1"
WAIT_SECONDS = 60
LOG_TAIL_BYTES = 256 * 1024


def docker(args: list[str], *, timeout: float, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    """One docker CLI call. A timeout becomes exit code 124 so the caller records it and still cleans up."""
    try:
        return subprocess.run([docker_path(), *args], capture_output=True, timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired as exc:
        message = f"docker {args[0]} timed out after {timeout:g} s".encode()
        return subprocess.CompletedProcess(exc.cmd, 124, stdout=exc.stdout or b"", stderr=(exc.stderr or b"") + message)


def docker_path() -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise RuntimeError("docker is not on PATH")
    return executable


def _text(data: bytes, limit: int = LOG_TAIL_BYTES) -> str:
    return data[-limit:].decode("utf-8", "replace")


def _redact(text: str, values: list[str]) -> str:
    for value in values:
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


def _image(name: str) -> dict[str, Any] | None:
    result = docker(["image", "inspect", name], timeout=30)
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout)
    return data[0] if data else None


def _snapshot(root: Path) -> dict[str, str]:
    """Relative path to a content fingerprint. Links are recorded, never followed."""
    found: dict[str, str] = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                found[rel] = "link"
            elif path.is_file():
                found[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                found[rel] = "dir"
    return found


def _changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changed = [f"+{p}" for p in after if p not in before]
    changed += [f"-{p}" for p in before if p not in after]
    changed += [f"~{p}" for p in after if p in before and after[p] != before[p]]
    return sorted(changed)


def _pairs(args: list[str]) -> list[str]:
    """One flag and its value per line, for reading; the image and command go last."""
    lines: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in ("--detach", "--read-only", "--init") or not args[i].startswith("--"):
            lines.append(" ".join(args[i:]) if not args[i].startswith("--") else args[i])
            if not args[i].startswith("--"):
                break
            i += 1
        else:
            lines.append(f"{args[i]} {args[i + 1]}")
            i += 2
    return lines


def _print_checks(label: str, checks: list[Check], stream: TextIO | None = None) -> None:
    print(f"\n== {label}", file=stream)
    for check in checks:
        print(f"  {check.id:<4} {check.verdict:<12} ({check.status}) {check.title}: {check.detail}", file=stream)


def finish_baseline(record: dict[str, Any], binary_sha: str, *, stream: TextIO) -> dict[str, Any]:
    """Save the raw evidence, analyse it, save the summary, and only then print. Returns the summary.

    Nothing printed can lose evidence: both files exist before the first line reaches the console."""
    record["binary_sha256"] = binary_sha
    report_path = _save(record["stamp"] + "-baseline.json", record)
    try:
        previous = json.loads((REPORTS / "latest-baseline-summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    try:
        checks = _analysis(record)
    except Exception as exc:  # the evidence is saved already; keep a bounded reason
        summary = {"mode": "baseline", "passed": False, "report_file": report_path.name,
                   "analysis_error": f"{type(exc).__name__}: {exc}"[:2000]}  # fmt: skip
        _save(record["stamp"] + "-baseline-summary.json", summary)
        print(f"Analysis failed: {summary['analysis_error']}\nReport: {report_path}", file=stream)
        return summary
    summary = _summary("baseline", record, checks, binary_sha)
    summary["report_file"] = report_path.name
    summary["previous_report_file"] = previous.get("report_file", "the summary before this run") if previous else None
    summary["changes_since_previous"] = compare_verdicts(previous, checks) if previous else []
    summary_path = _save(record["stamp"] + "-baseline-summary.json", summary)
    _save("latest-baseline-summary.json", summary)

    _print_checks("Run 1, baseline", checks, stream)
    counts = {v: sum(c.verdict == v for c in checks) for v in ("PASS", "FAIL", "NOT VERIFIED")}
    print(f"\nTotals: {counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['NOT VERIFIED']} NOT VERIFIED", file=stream)
    if previous:
        changes = summary["changes_since_previous"] or ["no verdict changed"]
        print("Changes since the previous baseline: " + "; ".join(changes), file=stream)
    print(f"Report:  {report_path}\nSummary: {summary_path}", file=stream)
    print(f"Duration: {record.get('duration_seconds')} s   Cleanup: {record.get('cleanup')}   "
          f"Server state at the end: {record.get('server_state')}", file=stream)  # fmt: skip
    return summary


def _summary(mode: str, record: dict[str, Any], checks: list[Check], binary_sha: str) -> dict[str, Any]:
    report = record.get("report") or {}
    health = (report.get("health") or {}).get("json") or {}
    doc = report.get("doc") or {}
    return {
        "mode": mode,
        "passed": not any(c.verdict == "FAIL" for c in checks),
        "binary_sha256": binary_sha,
        "image_id": record.get("image_id"),
        "server_version": health.get("version") if isinstance(health, dict) else None,
        "doc_format": {"status": doc.get("status"), "content_type": (doc.get("headers") or {}).get("content-type"),
                       "json": "json" in doc, "bytes": doc.get("bytes")},  # fmt: skip
        "duration_seconds": record.get("duration_seconds"),
        "isolation": record.get("isolation"),
        "cleanup": record.get("cleanup"),
        "checks": [{**c.__dict__, "verdict": c.verdict} for c in checks],
        "uncertainties": [f"{c.id} {c.title}: {c.detail}" for c in checks if c.verdict != "PASS"],
    }


def _container(
    run_dir: Path, binary_dir: Path, *, project_config_off: bool, tmp_exec: bool, guard: bool = False, project_opencode: bool = False
) -> CheckContainer:
    return CheckContainer(
        name=f"devpilot-opencode-check-{secrets.token_hex(4)}",
        binary_dir=binary_dir,
        config_dir=run_dir / "etc-opencode",
        checks_dir=run_dir / "checks",
        workspace_dir=run_dir / "workspace",
        project_config_off=project_config_off,
        tmp_exec=tmp_exec,
        guard_config_dir=run_dir / "global-config" if guard else None,
        guard_project_opencode=guard and project_opencode,
    )


def _prepare(c: CheckContainer, files: dict[str, str], check_files: dict[str, bytes] | None = None) -> None:
    for folder in (c.config_dir, c.checks_dir, c.workspace_dir):
        folder.mkdir(parents=True, exist_ok=False)
    if c.guard_config_dir is not None:
        c.guard_config_dir.mkdir(parents=True, exist_ok=False)
        (c.guard_config_dir / ".gitignore").write_text(OPENCODE_GITIGNORE, encoding="utf-8")
    config = managed_config(model=CHECK_MODEL, gateway_base_url=CHECK_GATEWAY)
    (c.config_dir / "opencode.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(PROBE, c.checks_dir / "probe.py")
    shutil.copyfile(RELAY, c.checks_dir / "http_relay.py")
    (c.checks_dir / "input.json").write_text(json.dumps(probe_input()) + "\n", encoding="utf-8")
    for rel, data in (check_files or {}).items():
        target = c.checks_dir.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for rel, text in files.items():
        target = c.workspace_dir.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def one_run(
    label: str,
    binary_dir: Path,
    files: dict[str, str],
    *,
    project_config_off: bool,
    tmp_exec: bool,
    guard: bool = False,
    exercise: Callable[[str, str], dict[str, Any]] | None = None,
    check_files: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    """Start one server, probe it, remove everything. Returns the redacted record.

    `exercise(container name, password)` replaces the probe, in the same container and with the same
    checks and cleanup. It must catch its own failures: the container is removed either way.
    `check_files` adds files to the read-only checks folder."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS / f"{stamp}-{label}-{secrets.token_hex(3)}"
    project_opencode = any(rel.startswith(".opencode/") for rel in files)
    c = _container(run_dir, binary_dir, project_config_off=project_config_off, tmp_exec=tmp_exec,
                   guard=guard, project_opencode=project_opencode)  # fmt: skip
    record: dict[str, Any] = {"label": label, "stamp": stamp, "container": c.name, "project_config_off": project_config_off,
                              "install_guard": guard, "mounts": [f"{t} ({'ro' if ro else 'rw'})" for _, t, ro in c.mounts()]}  # fmt: skip
    began = time.monotonic()
    password = secrets.token_urlsafe(32)
    token = base64.b64encode(f"opencode:{password}".encode()).decode()
    env = {**os.environ, PASSWORD_ENV: password}
    try:
        _prepare(c, files, check_files)
        problems = mount_problems(c, check_root=CHECK_ROOT, binary_dir=binary_dir, forbidden=[REPO_ROOT, Path.home()])
        if problems:
            record["error"] = "refused mounts: " + "; ".join(problems)
            return record
        before = _snapshot(c.workspace_dir)
        started = docker(build_run_args(c), timeout=120, env=env)
        if started.returncode != 0:
            record["error"] = "docker run failed: " + _redact(_text(started.stderr, 4000), [password, token])
            return record
        inspect = json.loads(docker(["inspect", c.name], timeout=30).stdout or b"[{}]")[0]
        record["isolation_problems"] = isolation_problems(inspect, c)
        record["isolation"] = isolation_summary(inspect)
        record["image_id"] = inspect.get("Image")
        if exercise is not None:
            record["report"] = exercise(c.name, password)
        else:
            probe = docker(build_exec_args(c.name, wait_seconds=WAIT_SECONDS), timeout=WAIT_SECONDS + 120)
            try:
                record["report"] = json.loads(probe.stdout)
            except ValueError:
                record["report"] = {"probe_error": f"exit {probe.returncode}: " + _text(probe.stdout + probe.stderr, 4000)}
        logs = docker(["logs", "--tail", "400", c.name], timeout=30)
        record["server_log_tail"] = _redact(_text(logs.stdout + logs.stderr), [password, token])
        state = docker(["inspect", "--format", "{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}", c.name], timeout=30)
        record["server_state"] = _text(state.stdout, 200).strip()
        record["workspace_changes"] = _changes(before, _snapshot(c.workspace_dir))
    finally:
        removed = docker(["rm", "--force", c.name], timeout=60)
        left = docker(["ps", "--all", "--quiet", "--filter", f"name={c.name}"], timeout=30)
        record["cleanup"] = {"removed": removed.returncode == 0 or b"No such container" in removed.stderr,
                             "left": _text(left.stdout, 200).split()}  # fmt: skip
        shutil.rmtree(run_dir, ignore_errors=True)
        record["cleanup"]["run_folder_removed"] = not run_dir.exists()
        record["duration_seconds"] = round(time.monotonic() - began, 1)
        del env, password
    serialized = json.dumps(record)
    if token in serialized:  # the password itself was removed above; this is a second guard
        record = json.loads(serialized.replace(token, "[REDACTED]"))
    return record


def _analysis(record: dict[str, Any]) -> list[Check]:
    if "error" in record and "report" not in record:
        checks = [Check("C1", "Server starts", FAILED, record["error"])]
    else:
        checks = analyze(
            record.get("report") or {},
            isolation=record.get("isolation_problems") or [],
            workspace_changes=record.get("workspace_changes") or [],
            expected_version=CONTRACT_VERSION,
            server_log=record.get("server_log_tail") or "",
        )
    return [*checks, cleanup_check(record.get("cleanup") or {})]


def _save(name: str, data: dict[str, Any]) -> Path:
    path = REPORTS / name
    write_report(path, data)  # UTF-8, atomic
    return path


def save_report(name: str, data: dict[str, Any]) -> Path:
    return _save(name, data)


def ready_binary() -> tuple[Path, str] | None:
    return _preflight(need_image=True)


def preflight_ok() -> bool:
    return _preflight(need_image=True) is not None


def sweep() -> list[str]:
    return _sweep()


def _preflight(*, need_image: bool) -> tuple[Path, str] | None:
    try:
        manifest, binary = verify_binary(MANIFEST)
    except ArtifactError as exc:
        print(f"STOPPED: {exc}")
        return None
    print(f"Binary: {binary}\n  matches its manifest: {manifest.binary_bytes} bytes, SHA-256 {manifest.binary_sha256}")
    if sorted(p.name for p in binary.parent.iterdir()) != ["opencode"]:
        print(f"STOPPED: {binary.parent} must hold only the binary")
        return None
    base = _image(BASE_IMAGE)
    check = _image(CHECK_IMAGE)
    print(f"Base image {BASE_IMAGE}: {base.get('Id') if base else 'ABSENT'}")
    print(f"Check image {CHECK_IMAGE}: {check.get('Id') if check else 'ABSENT (build it with --build-image, after approval)'}")
    if need_image and check and check.get("Id") != CHECK_IMAGE_ID:
        print(f"STOPPED: the check image is not the one built and recorded ({CHECK_IMAGE_ID}); it is not rebuilt automatically")
        return None
    if check and base:
        problems = image_problems(check, base)
        print("  provenance: " + ("; ".join(problems) if problems else "the pinned base plus one layer, user 65534"))
        if problems:
            return None
    if need_image and not (check and base):
        print("STOPPED: the check image is not built, or its base image is missing")
        return None
    return binary.parent, manifest.binary_sha256


def dry_run() -> int:
    print("OpenCode contract check: DRY RUN. Nothing is started, built or downloaded.\n")
    ready = _preflight(need_image=False)
    if ready is None:
        return 1
    binary_dir, _ = ready
    sample = RUNS / "<stamp>-<label>-<random>"
    for label, off, files in (
        ("Run 1, baseline (empty workspace), install guard", True, {}),
        ("Run 2a, precedence: hostile project config, project config allowed, install guard", False, hostile_workspace()),
        ("Run 2b, precedence: same files, OPENCODE_DISABLE_PROJECT_CONFIG=1, install guard", True, hostile_workspace()),
    ):
        c = _container(sample, binary_dir, project_config_off=off, tmp_exec=False, guard=True, project_opencode=bool(files))
        print(f"\n== {label}\ndocker run")
        for line in _pairs(build_run_args(c)[1:]):
            print(f"  {line}")
        print("then: docker " + " ".join(build_exec_args("<container>", wait_seconds=WAIT_SECONDS)))
        if files:
            print("workspace files (inert text):")
            for rel, text in files.items():
                print(f"  --- {rel}\n" + "".join(f"    {line}\n" for line in text.splitlines()))
    names = [n for n, _ in SERVER_ENV] + [PROJECT_CONFIG_OFF[0]]
    print(f"\nEnvironment set in the container: {', '.join(names)}, and {PASSWORD_ENV} (fresh random value, passed by name)")
    print("\nManaged config mounted at /etc/opencode/opencode.json:")
    print(json.dumps(managed_config(model=CHECK_MODEL, gateway_base_url=CHECK_GATEWAY), indent=2))
    print(f"\nCleanup on every path: docker rm --force <container>; delete {RUNS}\\<run>; keep the redacted report in {REPORTS}")
    return 0


def run(mode: str, *, tmp_exec: bool) -> int:
    ready = _preflight(need_image=True)
    if ready is None:
        return 1
    binary_dir, binary_sha = ready
    _sweep()
    if mode == "baseline":
        record = one_run("baseline", binary_dir, {}, project_config_off=True, tmp_exec=tmp_exec, guard=True)
        summary = finish_baseline(record, binary_sha, stream=sys.stdout)
        return 0 if summary["passed"] else 2

    latest = REPORTS / "latest-baseline-summary.json"
    try:
        baseline = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        baseline = {}
    if baseline.get("passed") is not True or baseline.get("binary_sha256") != binary_sha:
        print("STOPPED: the precedence run needs a passing baseline run of this exact binary first")
        return 1
    return run_precedence(binary_dir, binary_sha, tmp_exec=tmp_exec, stream=sys.stdout)


def run_precedence(binary_dir: Path, binary_sha: str, *, tmp_exec: bool, stream: TextIO) -> int:
    """Run A (project config on), then B (OPENCODE_DISABLE_PROJECT_CONFIG=1), both with the install guard.
    B does not start unless A cleaned up completely. Evidence is saved before anything is printed."""
    allowed = one_run("precedence-allowed", binary_dir, hostile_workspace(), project_config_off=False, tmp_exec=tmp_exec, guard=True)
    allowed["binary_sha256"] = binary_sha
    allowed_path = _save(allowed["stamp"] + "-precedence-allowed.json", allowed)
    if cleanup_check(allowed.get("cleanup") or {}).verdict != "PASS":
        print(f"STOPPED: run A did not clean up ({allowed.get('cleanup')}); run B was not started. Report: {allowed_path}", file=stream)
        return 1
    disabled = one_run("precedence-disabled", binary_dir, hostile_workspace(), project_config_off=True, tmp_exec=tmp_exec, guard=True)
    disabled["binary_sha256"] = binary_sha
    disabled_path = _save(disabled["stamp"] + "-precedence-disabled.json", disabled)

    runs = {"A": (allowed, _analysis(allowed)), "B": (disabled, _analysis(disabled))}
    checks = analyze_precedence(allowed.get("report") or {}, disabled.get("report") or {})
    summary = {
        "binary_sha256": binary_sha,
        "image_id": allowed.get("image_id"),
        "report_files": [allowed_path.name, disabled_path.name],
        "precedence_checks": [{**c.__dict__, "verdict": c.verdict} for c in checks],
        "contract_checks": {key: [{"id": c.id, "verdict": c.verdict, "detail": c.detail} for c in run_checks]
                            for key, (_, run_checks) in runs.items()},  # fmt: skip
        "comparison": precedence_comparison(allowed, disabled),
    }
    summary_path = _save(allowed["stamp"] + "-precedence-summary.json", summary)
    markdown_path = REPORTS / (allowed["stamp"] + "-precedence-report.md")
    _write_text_atomic(markdown_path, _precedence_markdown(summary))

    for key, (record, run_checks) in runs.items():
        _print_checks(f"Run {key}: {record['label']} (contract checks)", run_checks, stream)
        print(f"Cleanup: {record.get('cleanup')}   Duration: {record.get('duration_seconds')} s", file=stream)
    _print_checks("Configuration precedence", checks, stream)
    print(f"\nReports: {allowed_path}\n         {disabled_path}\nSummary: {summary_path}\nComparison: {markdown_path}", file=stream)
    return 0


def _write_text_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _precedence_markdown(summary: dict[str, Any]) -> str:
    on, off = summary["comparison"]["project_config_enabled"], summary["comparison"]["project_config_disabled"]
    lines = ["# OpenCode 1.19.5 configuration-precedence comparison", "",
             f"Binary SHA-256 `{summary['binary_sha256']}`, image `{summary['image_id']}`.", "",
             "| | A: project config enabled | B: OPENCODE_DISABLE_PROJECT_CONFIG=1 |", "|---|---|---|"]  # fmt: skip

    def row(label: str, a: Any, b: Any) -> None:
        lines.append(f"| {label} | {a} | {b} |")

    row("Project opencode.json loaded (marker)", on["project_file_loaded"], off["project_file_loaded"])
    row("Agents listed by GET /agent", ", ".join(on["agents_listed"]), ", ".join(off["agents_listed"]))
    for key in (on.get("enforced_decisions") or {}):
        row(f"Enforced: {key}", (on.get("enforced_decisions") or {}).get(key), (off.get("enforced_decisions") or {}).get(key))
    row("Wildcard permission rules enforced", "; ".join(on.get("wildcard_permission_rules") or []) or "none",
        "; ".join(off.get("wildcard_permission_rules") or []) or "none")  # fmt: skip
    row("Declared-config findings (GET /config)", len(on["declared_problems"]), len(off["declared_problems"]))
    row("Enforced-rule findings (GET /agent)", len(on["enforced_problems"]), len(off["enforced_problems"]))
    row("DevPilot verdict", on["devpilot_verdict"], off["devpilot_verdict"])
    row("Dependency install (C16)", on["install_check"]["verdict"], off["install_check"]["verdict"])
    row("Isolation problems (docker inspect)", on["isolation_problems"] or "none", off["isolation_problems"] or "none")
    row("Workspace changes", on["workspace_changes"] or "none", off["workspace_changes"] or "none")
    row("Cleanup", on["cleanup"], off["cleanup"])
    lines += ["", "## Precedence checks", ""]
    lines += [f"- **{c['id']} {c['verdict']}** {c['title']}: {c['detail']}" for c in summary["precedence_checks"]]
    for label, facts in (("A", on), ("B", off)):
        lines += ["", f"## DevPilot findings, run {label}", ""]
        lines += [f"- {p}" for p in facts["devpilot_problems"]] or ["- none"]
    return "\n".join(lines) + "\n"


def build_image() -> int:
    if _image(CHECK_IMAGE):
        print(f"{CHECK_IMAGE} already exists; nothing to build.")
        return 0
    if sorted(p.name for p in IMAGE_CONTEXT.iterdir()) != ["Dockerfile"]:
        print(f"STOPPED: the build context {IMAGE_CONTEXT} must hold only the Dockerfile")
        return 1
    started = time.monotonic()
    result = docker(["build", "--pull=false", "--progress", "plain", "--tag", CHECK_IMAGE, str(IMAGE_CONTEXT)], timeout=600)
    output = _text(result.stdout + result.stderr, 256 * 1024)
    for line in output.splitlines():
        if any(key in line for key in ("FROM", "fetch ", "Installing", "Upgrading", "Executing", "OK:", "ERROR", "error")):
            print(f"  {line.strip()}")
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "build-image.log").write_text(output, encoding="utf-8")
    if result.returncode != 0:
        print(f"STOPPED: the build failed (log: {REPORTS / 'build-image.log'})")
        return 1
    installed, problems = apk_changes(output)
    check, base = _image(CHECK_IMAGE), _image(BASE_IMAGE)
    problems += image_problems(check or {}, base or {})
    if problems:
        docker(["image", "rm", CHECK_IMAGE], timeout=120)
        print("STOPPED, image removed: " + "; ".join(problems))
        return 1
    record = {
        "image": CHECK_IMAGE,
        "image_id": (check or {}).get("Id"),
        "image_size_bytes": (check or {}).get("Size"),
        "base_image_id": (base or {}).get("Id"),
        "packages": installed,
        "layers": len(((check or {}).get("RootFS") or {}).get("Layers") or []),
        "build_seconds": round(time.monotonic() - started, 1),
    }
    _save("build-image.json", record)
    print(f"Built {CHECK_IMAGE}: {record['image_id']}, {record['image_size_bytes']} bytes")
    print(f"  packages: {installed}")
    print("  provenance: the pinned base plus one layer, user 65534")
    return 0


def _sweep() -> list[str]:
    """Remove containers left by an earlier check (they carry this check's label, nothing else does)."""
    listed = docker(["ps", "--all", "--quiet", "--filter", f"label={CHECK_LABEL}=true"], timeout=30)
    ids = _text(listed.stdout, 4000).split()
    for container in ids:
        docker(["rm", "--force", container], timeout=60)
    if ids:
        print(f"Removed {len(ids)} leftover check container(s)")
    return ids


def cleanup() -> int:
    _sweep()
    if RUNS.exists():
        for folder in RUNS.iterdir():
            shutil.rmtree(folder, ignore_errors=True)
    left = docker(["ps", "--all", "--quiet", "--filter", f"label={CHECK_LABEL}=true"], timeout=30)
    print(f"Containers left: {_text(left.stdout, 200).split() or 'none'}; run folders left: "
          f"{[p.name for p in RUNS.iterdir()] if RUNS.exists() else 'none'}")  # fmt: skip
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--run", choices=("baseline", "precedence"), help="start real containers (needs approval)")
    action.add_argument("--build-image", action="store_true", help="build the check image (needs approval)")
    action.add_argument("--cleanup", action="store_true", help="remove this check's containers and run folders")
    parser.add_argument("--allow-tmp-exec", action="store_true", help="allow exec on the /tmp tmpfs (needs approval)")
    args = parser.parse_args()
    # The Windows console (cp1252) cannot show every character a server may return; replace, never crash.
    safe_console(sys.stdout)
    if args.build_image:
        return build_image()
    if args.cleanup:
        return cleanup()
    if args.run:
        return run(args.run, tmp_exec=args.allow_tmp_exec)
    return dry_run()


if __name__ == "__main__":
    sys.exit(main())
