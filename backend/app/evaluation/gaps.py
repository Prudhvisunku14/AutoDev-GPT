"""Conservative source-to-requirements triage.

Candidate files are NOT proof that a requirement is implemented. This module avoids
false success claims and never executes code or reads outside WorkspaceFS.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

_WORD = re.compile(r"[a-z][a-z0-9_]{2,}", re.I)
_SKIP = frozenset({"with", "that", "this", "from", "must", "have", "user", "users", "application", "system", "should", "where", "will", "create", "develop", "provide", "using", "into", "data", "able", "test", "tests", "working"})
_EXTENSIONS = (".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".json", ".yml", ".yaml", ".toml", ".md", ".txt", ".csv")
_UNSAFE_NAMES = (".env", "credentials", "secret", "token", ".git/", "node_modules/", ".venv/", "__pycache__/")
MAX_FILES = 180
MAX_BYTES = 400_000


def _tokens(value: str) -> set[str]:
    return {s.lower() for s in _WORD.findall(value) if s.lower() not in _SKIP}


def _candidate(path: str) -> bool:
    low = path.lower()
    return low.endswith(_EXTENSIONS) and not any(v in low for v in _UNSAFE_NAMES) and ".." not in path.split("/")


def compare_requirements(
    requirements: Iterable[tuple[str, str]], paths: Iterable[str], read_bytes: Callable[[str], bytes]
) -> dict[str, object]:
    """Find possible code evidence, without claiming implementation or passing tests."""
    reqs = list(requirements)
    if not 1 <= len(reqs) <= 30 or len({code for code, _ in reqs}) != len(reqs):
        raise ValueError("provide between 1 and 30 requirements with distinct identifiers")
    paths = sorted(p for p in set(paths) if _candidate(p))[:MAX_FILES]
    indexed: list[tuple[str, set[str], str]] = []
    for path in paths:
        try:
            raw = read_bytes(path)
        except (OSError, KeyError, UnicodeError):
            continue
        if len(raw) > MAX_BYTES or b"\x00" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        indexed.append((path, _tokens(path.replace("/", " ") + " " + text), hashlib.sha256(raw).hexdigest()))
    rows = []
    for code, requirement in reqs:
        terms = _tokens(requirement)
        ranked = []
        for path, words, digest in indexed:
            matched = terms & words
            if matched:
                ranked.append({"path": path, "matching_terms": sorted(matched)[:8], "source_sha256": digest})
        ranked.sort(key=lambda x: (-len(x["matching_terms"]), x["path"]))
        rows.append({
            "requirement_id": code,
            "requirement": requirement,
            "status": "unverified_candidate" if ranked else "no_source_evidence_found",
            "candidate_files": ranked[:5],
            "next_step": "Inspect candidate code and write/run an independent acceptance test. A word match is not implementation proof." if ranked else "Plan code changes, then build and run independent tests.",
        })
    return {"method": "conservative lexical triage (not semantic verification)", "files_inspected": len(indexed), "requirements": rows, "verified_requirements": 0}


@dataclass(frozen=True)
class VersionChange:
    change_type: str
    before: str
    after: str
    previous_version: str
    current_version: str

    def as_dict(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in ("change_type", "before", "after", "previous_version", "current_version")}


def compare_document_versions(old: str, new: str, previous_version: str, current_version: str) -> dict[str, object]:
    """Exact document text difference; interpretation is deliberately separate."""
    if len(old) > 150_000 or len(new) > 150_000:
        raise ValueError("document text exceeds the allowed comparison size")
    before, after = old.splitlines(), new.splitlines()
    changes: list[dict[str, str]] = []
    for op, a, b, c, d in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        change = VersionChange(op, "\n".join(before[a:b])[:1000], "\n".join(after[c:d])[:1000], previous_version, current_version)
        changes.append(change.as_dict())
        if len(changes) >= 100:
            break
    return {"previous_version": previous_version, "current_version": current_version, "exact_changes": changes, "changed": bool(changes), "truncated": len(changes) >= 100, "interpretation": "not performed", "note": "These are observed text differences, not claims about policy or the latest authoritative document."}
