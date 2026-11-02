"""Where repository data comes from: a `RepositorySource`. Fixtures for tests and the offline demonstration; GitHub
for a live run, once its access is approved.

`GitHubSource` reads public data only and sends no token. Every URL it would fetch, redirects included, must first
pass the egress allowlist's `github` component, and every answer has a size limit. It has never been run against
GitHub: its tests use an in-process transport, so they make no network call.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Protocol
from urllib.parse import quote, urljoin

import httpx

from app.core.egress import EgressPolicy
from app.repos.model import COMMIT, FULL_NAME, GITHUB, RepoDetail, RepoSummary

if TYPE_CHECKING:
    from app.config import Settings

_MANIFEST_NAMES = ("package.json", "requirements.txt", "requirements-dev.txt", "pyproject.toml")
_MAX_MANIFESTS, _MAX_DEPTH, _MAX_REDIRECTS = 6, 2, 3


class RepositoryError(Exception):
    pass


class RepositoryUnavailable(RepositoryError):
    """Not found, removed, blocked or unreadable: an evaluation records it and moves on."""


class RepositoryAccessRefused(RepositoryError):
    """Policy refused the request before anything was sent. Nothing is retried."""


class RepositorySource(Protocol):
    name: str

    def search(self, query: str, *, limit: int) -> list[RepoSummary]: ...

    def detail(self, full_name: str) -> RepoDetail: ...

    def archive(self, full_name: str, commit: str, *, max_bytes: int) -> bytes:
        """A gzip tarball of exactly that commit."""
        ...


def check_names(full_name: str, commit: str | None = None) -> None:
    if not FULL_NAME.fullmatch(full_name):
        raise RepositoryAccessRefused(f"{full_name!r} is not an owner/name pair")
    if commit is not None and not COMMIT.fullmatch(commit):
        raise RepositoryAccessRefused("a commit is pinned by its full 40-character SHA")


class GitHubSource:
    name = GITHUB
    API = "https://api.github.com"

    def __init__(self, egress: EgressPolicy, *, client: httpx.Client | None = None, timeout: float = 20.0, max_text_bytes: int = 200_000) -> None:
        self._egress = egress
        self._client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "DevPilot-AI", "X-GitHub-Api-Version": "2022-11-28"})
        self._max_text = max_text_bytes

    def _get(self, url: str, *, accept: str = "application/vnd.github+json", max_bytes: int | None = None) -> bytes:
        limit = max_bytes or self._max_text
        for _hop in range(_MAX_REDIRECTS + 1):
            decision = self._egress.check("github", url)
            if not decision.allowed:
                raise RepositoryAccessRefused(f"egress policy refused {httpx.URL(url).host}: {decision.reason}")
            with self._client.stream("GET", url, headers={"Accept": accept}, follow_redirects=False) as answer:
                if answer.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, answer.headers.get("location", ""))
                    continue
                if answer.status_code in (403, 404, 409, 410, 451):
                    raise RepositoryUnavailable(f"GitHub answered {answer.status_code}")
                if answer.status_code != 200:
                    raise RepositoryUnavailable(f"GitHub answered {answer.status_code}")
                body = b""
                for chunk in answer.iter_bytes():
                    body += chunk
                    if len(body) > limit:
                        raise RepositoryUnavailable(f"the answer is over the {limit}-byte limit")
                return body
        raise RepositoryUnavailable("too many redirects")

    def _json(self, path: str) -> dict:
        return json.loads(self._get(f"{self.API}{path}"))

    def search(self, query: str, *, limit: int) -> list[RepoSummary]:
        data = self._json(f"/search/repositories?q={quote(query)}&per_page={max(1, min(limit, 20))}")
        return [_summary(item) for item in data.get("items", [])][:limit]

    def detail(self, full_name: str) -> RepoDetail:
        check_names(full_name)
        summary = _summary(self._json(f"/repos/{full_name}"))
        commit = self._get(f"{self.API}/repos/{full_name}/commits/{quote(summary.default_branch)}", accept="application/vnd.github.sha").decode().strip()
        check_names(full_name, commit)
        tree = self._json(f"/repos/{full_name}/git/trees/{commit}?recursive=1")
        files = {e["path"]: int(e.get("size", 0)) for e in tree.get("tree", []) if e.get("type") == "blob"}
        raw = "application/vnd.github.raw"
        readme = _text_or_empty(lambda: self._get(f"{self.API}/repos/{full_name}/readme?ref={commit}", accept=raw))
        license_text = _text_or_empty(lambda: self._get(f"{self.API}/repos/{full_name}/license?ref={commit}", accept=raw))
        wanted = [p for p in sorted(files) if p.rsplit("/", 1)[-1] in _MANIFEST_NAMES and p.count("/") <= _MAX_DEPTH][:_MAX_MANIFESTS]
        manifests = {p: _text_or_empty(lambda p=p: self._get(f"{self.API}/repos/{full_name}/contents/{quote(p)}?ref={commit}", accept=raw)) for p in wanted}
        return RepoDetail(summary, commit, files, readme, manifests, license_text, GITHUB, truncated=bool(tree.get("truncated")))

    def archive(self, full_name: str, commit: str, *, max_bytes: int) -> bytes:
        """GitHub redirects this to codeload.github.com, which the egress allowlist must name for it to succeed."""
        check_names(full_name, commit)
        return self._get(f"{self.API}/repos/{full_name}/tarball/{commit}", accept="application/vnd.github+json", max_bytes=max_bytes)


def build_repository_source(settings: Settings, egress: EgressPolicy) -> RepositorySource | None:
    """REPOSITORY_SOURCE=github wires the GitHub client; "off", the default, wires nothing. No token is read."""
    return GitHubSource(egress) if settings.repository_source == GITHUB else None


def _text_or_empty(read: object) -> str:
    try:
        return read().decode("utf-8", errors="replace")  # type: ignore[operator]
    except RepositoryUnavailable:
        return ""


def _summary(item: dict) -> RepoSummary:
    license_info = item.get("license") or {}
    return RepoSummary(
        full_name=item["full_name"], url=item.get("html_url", f"https://github.com/{item['full_name']}"), description=item.get("description") or "",
        language=item.get("language"), stars=int(item.get("stargazers_count", 0)), license=license_info.get("spdx_id"), archived=bool(item.get("archived")),
        pushed_at=item.get("pushed_at"), default_branch=item.get("default_branch") or "main", size_kb=int(item.get("size", 0)), topics=tuple(item.get("topics") or ()),
    )  # fmt: skip
