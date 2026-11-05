"""Approval-gated GitHub draft pull requests for an already verified, delivered DevPilot run (from the evaluation upgrade).

The person previews exactly what would be uploaded (every file with its SHA-256), then types a confirmation naming the repository
and branch. Only one new `devpilot/*` branch is created, with one commit and a **draft** pull request: no arbitrary URL, git
command, force-push, merge or deployment. The write itself goes through `repos.github_write.GitHubWriter`, which checks the
repository's identity and push permission, refuses an existing branch, and makes the commit before the branch exists, so a
failed upload leaves no half-made branch.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass

from app.repos.github_write import GitHubWriteError, GitHubWriter

REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
BRANCH = re.compile(r"^devpilot/[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
MAX_FILES = 300
MAX_TOTAL_BYTES = 8_000_000
MAX_FILE_BYTES = 2_000_000
PLAN_TTL_SECONDS = 600


class PublishRefused(Exception):
    """Safe-to-display summary. Never holds credentials or remote response bodies."""


@dataclass(frozen=True)
class UploadFile:
    path: str
    content: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class PublishPlan:
    run_id: str
    repository: str
    base: str
    branch: str
    title: str
    body: str
    files: tuple[UploadFile, ...]
    fingerprint: str

    def summary(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "repository": self.repository,
            "base": self.base,
            "branch": self.branch,
            "title": self.title,
            "files": [{"path": f.path, "bytes": len(f.content), "sha256": f.digest} for f in self.files],
            "total_bytes": sum(len(f.content) for f in self.files),
            "fingerprint": self.fingerprint,
            "will_create": ["new development branch", "one commit", "draft pull request"],
            "will_not_do": ["merge", "deploy", "force push", "delete files", "modify the base branch"],
        }


def _clean_path(path: str) -> str:
    parts = path.split("/")
    if not path or len(path) > 250 or any(s in ("", ".", "..") for s in parts) or "\\" in path or path.startswith("/"):
        raise PublishRefused("a source path is invalid or unsafe")
    if any(s in (".git", ".venv", ".opencode", "node_modules") for s in parts):
        raise PublishRefused("source contains an internal tooling path")
    if any(s.lower() == ".env" or (s.lower().startswith(".env.") and s.lower() != ".env.example") for s in parts):
        raise PublishRefused("an environment-secret file must not be published")
    return path


def build_plan(run_id: str, repository: str, base: str, branch: str, title: str, body: str, files: dict[str, bytes]) -> PublishPlan:
    if not REPO.fullmatch(repository) or ".." in repository or repository.split("/")[1].startswith("."):
        raise PublishRefused("repository must be owner/name with a valid GitHub name")
    if not BRANCH.fullmatch(branch) or ".." in branch or "//" in branch or branch.endswith(("/", ".", ".lock")) or "/." in branch:
        raise PublishRefused("the new branch must be a valid devpilot/* branch")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,120}", base) or ".." in base or "//" in base or "/." in base:
        raise PublishRefused("base branch name is invalid")
    if base == branch:
        raise PublishRefused("development and base branches must differ")
    if not 4 <= len(title.strip()) <= 160 or len(body) > 10_000:
        raise PublishRefused("title must be 4-160 characters and body at most 10,000 characters")
    if not 1 <= len(files) <= MAX_FILES:
        raise PublishRefused("publish at least one and at most 300 verified files")
    if sum(len(content) for content in files.values()) > MAX_TOTAL_BYTES:
        raise PublishRefused("verified artifact exceeds 8 MB GitHub publishing limit")
    ordered: list[UploadFile] = []
    for name, content in sorted(files.items()):
        _clean_path(name)
        if len(content) > MAX_FILE_BYTES:
            raise PublishRefused("one source file exceeds 2 MB GitHub publishing limit")
        ordered.append(UploadFile(name, content))
    fingerprint_data = [run_id, repository, base, branch, title.strip(), body]
    fingerprint_data.extend(f"{f.path}:{f.digest}" for f in ordered)
    fingerprint = hashlib.sha256("\n".join(fingerprint_data).encode("utf-8")).hexdigest()
    return PublishPlan(run_id, repository, base, branch, title.strip(), body, tuple(ordered), fingerprint)


class PreviewStore:
    """Short-lived, in-memory, one-use confirmation. Restart cancels outstanding previews."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._items: dict[str, tuple[float, PublishPlan]] = {}

    def save(self, plan: PublishPlan) -> str:
        key = secrets.token_urlsafe(24)
        with self._mutex:
            now = time.monotonic()
            self._items = {k: v for k, v in self._items.items() if v[0] > now}
            self._items[key] = (now + PLAN_TTL_SECONDS, plan)
        return key

    def claim(self, preview_id: str) -> PublishPlan:
        with self._mutex:
            found = self._items.pop(preview_id, None)
        if found is None or found[0] <= time.monotonic():
            raise PublishRefused("the confirmation preview expired or has already been used")
        return found[1]


def publish(plan: PublishPlan, writer: GitHubWriter) -> dict[str, str]:
    """One commit on a new branch, and a draft pull request. Every refusal comes back as PublishRefused, safe to display."""
    try:
        published = writer.open_pull_request(plan.repository, files={f.path: f.content for f in plan.files}, branch=plan.branch, base=plan.base,
                                             title=plan.title, body=plan.body, message=f"DevPilot: {plan.title}", draft=True)  # fmt: skip
    except GitHubWriteError as exc:
        raise PublishRefused(f"GitHub did not accept the draft pull request: {exc}") from exc
    if not published.pull_request_url.startswith(f"https://github.com/{plan.repository}/pull/"):
        raise PublishRefused("GitHub created the pull request but returned an unexpected URL; inspect the repository")
    return {"url": published.pull_request_url, "branch": published.branch, "commit": published.commit, "repository": published.repository, "draft": "true"}
