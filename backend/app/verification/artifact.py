"""The downloadable copy of a verified project: its files, and the report that says how they were verified.

Only a run that COMPLETED and was verified is packaged, with nothing simulated. With an evaluator's independent
acceptance suite, that suite must have PASSED, and a failed one blocks the download. A freeform request has no such
suite; its delivery is then offered only when the project's own tests passed in the sandbox, and it is labelled as
verified by the generated tests only (`verification_level`). What may never leave the workspace is left out by name (environment
files, keys, version-control and tool folders, caches, local databases) and by content (a configured secret or a
credential format, `Redactor.contains_secret`). If that would omit source or project configuration, delivery is blocked
instead of claiming the incomplete archive is the verified project. The manifest lists excluded files when delivery is safe.

Pure functions over (path, bytes) pairs: the callers read the files through WorkspaceFS, which keeps every path jailed.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from app.core.redaction import Redactor
from app.verification.report import render_markdown

MAX_ARCHIVE_BYTES = 50_000_000  # the project's files together, before compression
REPORT_FOLDER = "devpilot-verification"

_EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".opencode", ".devpilot", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".vscode", ".cache", "dist", ".next",
})  # fmt: skip
_EXCLUDED_NAMES = frozenset({".npmrc", ".pypirc", ".netrc", ".git-credentials", "opencode.json", "opencode.jsonc", ".DS_Store", "Thumbs.db"})
_EXCLUDED_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm", ".log")
_KEY_FILE = re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$")
_SOURCE_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".html", ".htm", ".css", ".scss", ".sass",
    ".json", ".toml", ".yaml", ".yml", ".sh", ".ps1", ".bat", ".cmd", ".go", ".rs", ".java", ".cs", ".php",
    ".md", ".txt", ".lock", ".ini", ".cfg", ".xml",
})  # fmt: skip


@dataclass(frozen=True)
class Entry:
    path: str
    size: int


@dataclass(frozen=True)
class Left:
    path: str
    reason: str


@dataclass(frozen=True)
class Selection:
    included: tuple[Entry, ...]
    excluded: tuple[Left, ...]

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.included)


def unavailable_reason(report: dict[str, Any] | None) -> str | None:
    """Why this run has nothing to download, or None when it is a verified delivery."""
    if not report:
        return "the run has no final report yet"
    run = report.get("run") or {}
    if run.get("state") != "COMPLETED":
        return f"the run ended {run.get('state', 'without a state')}, so nothing verified was delivered"
    acceptance = report.get("acceptance") or {}
    if acceptance.get("configured"):
        if acceptance.get("verdict") != "PASSED":
            return f"independent acceptance did not pass (verdict {acceptance.get('verdict') or 'unknown'})"
    elif not _own_tests_passed(report):
        return "no independent acceptance suite judged this run, and the generated tests did not pass in the sandbox"
    simulated = (report.get("provenance") or {}).get("simulated") or []
    if simulated:
        return f"the run used simulated components ({', '.join(map(str, simulated))}), so its files are not a real verified delivery"
    if not report.get("delivered_files"):
        return "the run delivered no files"
    return None


INDEPENDENT, GENERATED_TESTS = "independent", "generated-tests"


def _own_tests_passed(report: dict[str, Any]) -> bool:
    """The last sandbox test run of the generated project passed, and ran at least one test."""
    runs = [v for v in report.get("verifications") or [] if isinstance(v, dict)]
    return bool(runs) and runs[-1].get("verdict") == "PASSED" and bool(runs[-1].get("ran"))


def verification_level(report: dict[str, Any] | None) -> str | None:
    """How a downloadable delivery was verified: by the evaluator's independent suite, or by the project's own tests
    only (a freeform request has no evaluator's suite). None when there is nothing to download."""
    if not report or unavailable_reason(report) is not None:
        return None
    return INDEPENDENT if (report.get("acceptance") or {}).get("configured") else GENERATED_TESTS


def name_rule(path: str) -> str | None:
    """Why a path is never packaged by its name alone, or None."""
    parts = path.split("/")
    for folder in parts[:-1]:
        if folder in _EXCLUDED_DIRS:
            return f"inside {folder}/, which is tool, cache or version-control data"
    name = parts[-1]
    lowered = name.lower()
    if lowered == ".env" or (lowered.startswith(".env.") and lowered != ".env.example"):
        return "an environment file, which can hold secrets"
    if name in _EXCLUDED_NAMES:
        return "a credential or tool configuration file"
    if _KEY_FILE.match(name):
        return "a key file"
    if lowered.endswith(_EXCLUDED_SUFFIXES):
        return "a key, compiled, log or local database file"
    return None


def omitted_source_reason(selection: Selection) -> str | None:
    """Do not offer a verified archive if a source/config file was removed by packaging safeguards."""
    omitted = [
        item.path for item in selection.excluded
        if PurePosixPath(item.path).suffix.lower() in _SOURCE_SUFFIXES
        and item.reason.startswith(("it contains", "could not be read", "the archive would pass"))
    ]
    if omitted:
        names = ", ".join(omitted[:3])
        more = f" and {len(omitted) - 3} more" if len(omitted) > 3 else ""
        return f"verified source file(s) {names}{more} could not be included safely; refusing to deliver an incomplete verified project"
    return None


def select(paths: Iterable[str], read: Callable[[str], bytes], redactor: Redactor) -> tuple[Selection, dict[str, bytes]]:
    """The files that may be packaged, read once. A file that cannot be read is left out, never guessed."""
    included: list[Entry] = []
    excluded: list[Left] = []
    data: dict[str, bytes] = {}
    total = 0
    for path in sorted(paths):
        reason = name_rule(path)
        if reason is not None:
            excluded.append(Left(path, reason))
            continue
        try:
            content = read(path)
        except Exception as exc:  # noqa: BLE001 - a jail refusal, a size limit or an I/O error: the file stays out
            excluded.append(Left(path, f"could not be read: {exc}"))
            continue
        if redactor.contains_secret(content.decode("utf-8", errors="ignore")):
            excluded.append(Left(path, "it contains a configured secret or a credential"))
            continue
        if total + len(content) > MAX_ARCHIVE_BYTES:
            excluded.append(Left(path, f"the archive would pass {MAX_ARCHIVE_BYTES // 1_000_000} MB"))
            continue
        total += len(content)
        included.append(Entry(path, len(content)))
        data[path] = content
    return Selection(tuple(included), tuple(excluded)), data


@dataclass(frozen=True)
class Delivery:
    """Whether a run may be packaged, and what: the one decision behind the dashboard's download and `devpilot build`."""

    reason: str | None  # None: a verified delivery that may be packaged
    selection: Selection
    data: dict[str, bytes]


def decide(report: dict[str, Any] | None, paths: Iterable[str], read: Callable[[str], bytes], redactor: Redactor) -> Delivery:
    """Every packaging rule, in order: a verified run, no source or configuration dropped by a safeguard, and something to package.
    Nothing is read when the run itself is not a verified delivery."""
    reason = unavailable_reason(report)
    if reason is not None:
        return Delivery(reason, Selection((), ()), {})
    selection, data = select(paths, read, redactor)
    reason = omitted_source_reason(selection)
    if reason is None and not selection.included:
        reason = "no file of the project can be packaged"
    return Delivery(reason, selection, data)


def archive_name(report: dict[str, Any]) -> str:
    project = (report.get("project") or {}).get("name") or "project"
    slug = re.sub(r"[^a-z0-9]+", "-", str(project).lower()).strip("-")[:48] or "project"
    run_id = str((report.get("run") or {}).get("id") or "run")[:8]
    return f"{slug}-{run_id}"


def build_zip(report: dict[str, Any], selection: Selection, data: dict[str, bytes], redactor: Redactor, *, when: datetime | None = None,
              provenance_note: str | None = None, extra: dict[str, str] | None = None) -> bytes:  # fmt: skip
    """The archive: the project's files under one folder, then the verification report and a manifest."""
    root = archive_name(report)
    stamp = (when or datetime(2026, 1, 1)).timetuple()[:6]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:

        def add(path: str, content: bytes) -> None:
            info = zipfile.ZipInfo(f"{root}/{path}", date_time=stamp)  # type: ignore[arg-type]
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)

        for entry in selection.included:
            add(entry.path, data[entry.path])
        for name, text in (extra or {}).items():  # DevPilot's own files (such as how to run the project), redacted like the report
            add(name, redactor.redact_text(text).encode("utf-8"))
        add(f"{REPORT_FOLDER}/report.md", redactor.redact_text(_markdown(report)).encode("utf-8"))
        add(f"{REPORT_FOLDER}/report.json", json.dumps(redactor.redact(report), indent=2, default=str).encode("utf-8"))
        add(f"{REPORT_FOLDER}/MANIFEST.txt", _manifest(report, selection, provenance_note).encode("utf-8"))
    return buffer.getvalue()


def _markdown(report: dict[str, Any]) -> str:
    """The rendered report. An older or partial report (a terminal build's summary.json) may lack a section the renderer
    expects; the archive is still delivered, and report.json beside it holds everything that was recorded."""
    try:
        return render_markdown(report)
    except (KeyError, TypeError, IndexError):
        project = (report.get("project") or {}).get("name") or "Project"
        state = (report.get("run") or {}).get("state") or "unknown"
        return f"# {project}: {state}\n\nThis report is incomplete, so it is not rendered here. report.json, beside this file, holds everything recorded.\n"


def _manifest(report: dict[str, Any], selection: Selection, provenance_note: str | None) -> str:
    acceptance = report.get("acceptance") or {}
    provenance = report.get("provenance") or {}
    own = [v for v in report.get("verifications") or [] if isinstance(v, dict)]
    how = (f"Independent acceptance: {acceptance.get('verdict')} ({acceptance.get('passed')} of {acceptance.get('total')})" if acceptance.get("configured")
           else f"Verified by the generated tests only (no independent acceptance suite): {own[-1].get('passed')} of {own[-1].get('ran')} passed" if own
           else "Not independently verified")  # fmt: skip
    lines = [
        f"Project: {(report.get('project') or {}).get('name')}",
        f"Run: {(report.get('run') or {}).get('id')} ({(report.get('run') or {}).get('state')})",
        how,
        f"Simulated parts: {', '.join(provenance.get('simulated') or []) or 'none'}",
    ]
    if provenance_note:
        lines.append(provenance_note)
    lines += ["", f"Included ({len(selection.included)} files, {selection.total_bytes} bytes):", *(f"  {e.path}  ({e.size} bytes)" for e in selection.included)]
    lines += ["", f"Left out ({len(selection.excluded)}):", *(f"  {left.path}: {left.reason}" for left in selection.excluded)] if selection.excluded else ["", "Left out: nothing"]
    return "\n".join(lines) + "\n"
