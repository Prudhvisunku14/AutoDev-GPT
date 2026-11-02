"""Judging a repository for reuse, and deciding between reuse and building from scratch. Python decides; GPT gets the result.

Nothing here is specific to one kind of application. Relevance comes from the words of the request; each required
technology must be shown by the repository's own manifests, not by its README's claims. Text from the repository is
scanned as untrusted input: instructions aimed at an AI agent, or commands that pipe a download into a shell, make a
repository ineligible, because the import never runs anything and a later step must not be talked into it either.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import date

from app.repos.licenses import LicenseVerdict, judge_license
from app.repos.model import RepoDetail, RepoSummary

HIGH, MEDIUM = "high", "medium"
_STOPWORDS = {"the", "and", "for", "with", "using", "use", "app", "application", "build", "add", "make", "create", "from", "that", "this", "into", "existing", "public",
              "repositories", "repository", "github", "search", "code", "run", "tests", "final", "deliver", "missing", "features", "reuse", "compatible", "suitable",
              "open", "source", "implementations", "evaluate", "repair", "failures", "simple", "small", "project"}  # fmt: skip
_DESCRIPTION_CHARS = 200


def _manifests(detail: RepoDetail, suffix: str) -> Iterable[str]:
    return (text for path, text in detail.manifests.items() if path.rsplit("/", 1)[-1].lower().startswith(suffix) or path.endswith(suffix))


def _npm(name: str) -> Callable[[RepoDetail], bool]:
    def found(detail: RepoDetail) -> bool:
        for text in _manifests(detail, "package.json"):
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if any(name in (data.get(key) or {}) for key in ("dependencies", "devDependencies", "peerDependencies")):
                return True
        return False

    return found


def _pip(name: str) -> Callable[[RepoDetail], bool]:
    pattern = re.compile(rf"(?im)^\s*[\"']?{re.escape(name)}(?![A-Za-z0-9_-])")
    return lambda detail: any(pattern.search(text) for text in [*_manifests(detail, "requirements"), *_manifests(detail, "pyproject.toml")])


def _files(*suffixes: str) -> Callable[[RepoDetail], bool]:
    return lambda detail: any(path.endswith(suffixes) for path in detail.files)


# A technology the request names is claimed only when the repository's own files show it.
TECHNOLOGIES: dict[str, Callable[[RepoDetail], bool]] = {
    "react": _npm("react"), "vue": _npm("vue"), "svelte": _npm("svelte"), "angular": _npm("@angular/core"), "express": _npm("express"), "next": _npm("next"),
    "vite": _npm("vite"), "fastapi": _pip("fastapi"), "flask": _pip("flask"), "django": _pip("django"), "python": _files(".py"),
    "typescript": _files(".ts", ".tsx"), "javascript": _files(".js", ".jsx", ".mjs"),
}  # fmt: skip

_INJECTION = [
    re.compile(r"(?i)ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts)"),
    re.compile(r"(?i)\b(ai|llm|coding)\s+(agents?|assistants?|models?)\b.{0,80}\b(must|should|need to|are required to)\b"),
    re.compile(r"(?i)(system prompt|developer message|disregard (your|the) (rules|instructions)|do not tell the user)"),
]
_PIPE_TO_SHELL = re.compile(r"(?i)\b(curl|wget|iwr|invoke-webrequest)\b[^\n|]{0,200}\|\s*(sudo\s+)?(sh|bash|zsh|python3?|iex|powershell)\b")
_INSTALL_HOOKS = ("preinstall", "install", "postinstall", "prepare")


@dataclass(frozen=True)
class Finding:
    severity: str
    where: str
    what: str


def scan_untrusted(detail: RepoDetail) -> list[Finding]:
    """What in the repository's text tries to direct an agent, or would run code on install. Nothing is executed."""
    findings: list[Finding] = []
    texts = {"README": detail.readme, **detail.manifests}
    for where, text in texts.items():
        if any(p.search(text) for p in _INJECTION):
            findings.append(Finding(HIGH, where, "text addressed to AI agents that tries to change their instructions"))
        if _PIPE_TO_SHELL.search(text):
            findings.append(Finding(HIGH, where, "a command that pipes a download into a shell"))
    for path, text in detail.manifests.items():
        if path.endswith("package.json"):
            try:
                scripts = json.loads(text).get("scripts") or {}
            except ValueError:
                scripts = {}
            hooks = sorted(set(scripts) & set(_INSTALL_HOOKS))
            if hooks:
                findings.append(Finding(MEDIUM, path, f"npm lifecycle scripts that run on install: {', '.join(hooks)} (never run by the import)"))
    if any(path.rsplit("/", 1)[-1] == "setup.py" for path in detail.files):
        findings.append(Finding(MEDIUM, "setup.py", "a setup.py runs code when installed (never run by the import)"))
    return findings


def terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9+#.-]{2,}", text.lower()) if w not in _STOPWORDS}


@dataclass(frozen=True)
class Limits:
    max_size_kb: int
    max_files: int


@dataclass(frozen=True)
class Evaluation:
    full_name: str
    url: str
    source: str
    available: bool
    eligible: bool
    score: float
    reasons: tuple[str, ...]
    commit: str | None = None
    license: LicenseVerdict | None = None
    relevance: float = 0.0
    evidence: dict[str, bool] = field(default_factory=dict)
    tests_present: bool = False
    findings: tuple[Finding, ...] = ()
    size_kb: int = 0
    file_count: int = 0
    description: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def for_gpt(self) -> dict[str, object]:
        """The evaluation, not the repository's words. The description is cut short and marked as untrusted."""
        return {
            "full_name": self.full_name, "url": self.url, "commit": self.commit, "eligible": self.eligible, "score": round(self.score, 3), "reasons": list(self.reasons),
            "license": self.license.spdx if self.license else None, "license_allowed": bool(self.license and self.license.allowed),
            "evidence": self.evidence, "tests_present": self.tests_present, "warnings": [f"{f.severity}: {f.where}: {f.what}" for f in self.findings],
            "untrusted_description": self.description[:_DESCRIPTION_CHARS],
        }  # fmt: skip


def unavailable(summary: RepoSummary, source: str, why: str) -> Evaluation:
    return Evaluation(summary.full_name, summary.url, source, False, False, 0.0, (f"unavailable: {why}",), description=summary.description)


def evaluate(detail: RepoDetail, *, query: str, must_have: Iterable[str], limits: Limits, today: date | None = None) -> Evaluation:
    s = detail.summary
    wanted = terms(query) | {t.lower() for t in must_have}
    words = terms(" ".join([s.full_name.replace("/", " ").replace("-", " "), s.description, " ".join(s.topics), detail.readme[:20_000]]))
    relevance = len(wanted & words) / len(wanted) if wanted else 0.0
    required = [t.lower() for t in must_have]
    evidence = {t: TECHNOLOGIES[t](detail) if t in TECHNOLOGIES else t in words for t in required}
    tests = any(re.search(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*\.py$|\.(test|spec)\.[jt]sx?$", path) for path in detail.files)
    findings = scan_untrusted(detail)
    verdict = judge_license(s.license, detail.license_text)
    reasons: list[str] = []
    if not verdict.allowed:
        reasons.append(f"licence: {verdict.reason}")
    missing = [t for t, shown in evidence.items() if not shown]
    if missing:
        reasons.append(f"not shown by the repository's own files: {', '.join(missing)}")
    if any(f.severity == HIGH for f in findings):
        reasons.append("untrusted instructions: " + "; ".join(f"{f.where}: {f.what}" for f in findings if f.severity == HIGH))
    if s.archived:
        reasons.append("archived: no longer maintained")
    if s.size_kb > limits.max_size_kb or len(detail.files) > limits.max_files:
        reasons.append(f"too large: {s.size_kb} KB and {len(detail.files)} files, over {limits.max_size_kb} KB or {limits.max_files} files")
    if detail.truncated:
        reasons.append("the host cut the file listing short, so the repository cannot be checked in full")
    recent = 0.0
    if s.pushed_at:
        age_days = ((today or date.today()) - date.fromisoformat(s.pushed_at[:10])).days
        recent = 1.0 if age_days <= 365 else 0.5 if age_days <= 730 else 0.0
    quality = 0.5 * tests + 0.25 * recent + 0.25 * min(math.log10(s.stars + 1) / 3, 1.0)
    coverage = sum(evidence.values()) / len(evidence) if evidence else 1.0
    score = 0.5 * relevance + 0.3 * coverage + 0.2 * quality
    return Evaluation(
        s.full_name, s.url, detail.source, True, not reasons, score, tuple(reasons) or ("meets every rule",), detail.commit, verdict, relevance, evidence, tests, tuple(findings),
        s.size_kb, len(detail.files), s.description,
    )  # fmt: skip


@dataclass(frozen=True)
class ReuseDecision:
    action: str  # "reuse" or "build"
    reason: str
    full_name: str | None = None
    commit: str | None = None
    url: str | None = None
    license: str | None = None
    attribution: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def decide(evaluations: list[Evaluation], *, threshold: float) -> ReuseDecision:
    """Reuse only an eligible repository good enough to start from; otherwise build from scratch, and say why."""
    eligible = sorted((e for e in evaluations if e.eligible), key=lambda e: (-round(e.score, 6), e.full_name))
    if not evaluations:
        return ReuseDecision("build", "no repository matched the search")
    if not eligible:
        return ReuseDecision("build", f"none of the {len(evaluations)} repositories found is eligible")
    best = eligible[0]
    if best.score < threshold:
        return ReuseDecision("build", f"the best eligible repository, {best.full_name}, scores {best.score:.2f}, under the {threshold:.2f} needed to start from it")
    assert best.license is not None and best.commit is not None
    return ReuseDecision("reuse", f"{best.full_name} is eligible and scores {best.score:.2f}", best.full_name, best.commit, best.url, best.license.spdx, best.license.attribution)
