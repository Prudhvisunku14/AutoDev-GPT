"""Reading a person's GitHub repository, public or private, at an exact commit.

- **Authorization.** A token is optional. It is sent only to `api.github.com`, as an `Authorization` header, never in a URL,
  never to a redirect target and never in any message. Without one, GitHub answers 404 for a private repository, so a 404 is
  reported as "not found, or private": it is never taken to mean the repository does not exist.
- **Network.** Every URL, redirects included, must pass the egress allowlist's `github` component. Archive downloads are
  redirected to `codeload.github.com` with a short-lived signed URL; that URL is followed without the token and is never
  shown, because its query is itself a credential.
- **Safety.** Every answer has a size limit. A repository is downloaded as the archive of one pinned commit and unpacked by
  `importer.read_archive`, which refuses links, `.git` paths, path escapes and files that could reconfigure the coding
  engine. Nothing downloaded is ever run here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from app.core.egress import EgressPolicy
from app.repos.github_url import RepoRef, check_ref
from app.repos.model import COMMIT

API_HOST = "api.github.com"
API = f"https://{API_HOST}"
_MAX_REDIRECTS = 3


@dataclass(frozen=True)
class AccessProblem:
    kind: str  # authorization_required | token_rejected | forbidden | rate_limited | not_visible | ref_not_found | too_large | refused | unreachable
    message: str


class RepoAccessError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.problem = AccessProblem(kind, message)

    @property
    def kind(self) -> str:
        return self.problem.kind

    @property
    def needs_authorization(self) -> bool:
        return self.kind in ("authorization_required", "token_rejected", "not_visible", "forbidden")


@dataclass(frozen=True)
class RepoInfo:
    full_name: str
    url: str
    private: bool
    default_branch: str
    archived: bool
    size_kb: int
    description: str  # untrusted
    license: str | None


def _status_problem(status: int, headers: httpx.Headers, *, has_token: bool, what: str) -> RepoAccessError:
    if status == 401:
        if has_token:
            return RepoAccessError("token_rejected", "GitHub rejected the token (HTTP 401): it is wrong, expired or revoked. Give a new one with `devpilot setup --github-token`")
        return RepoAccessError("authorization_required", "GitHub requires authorization (HTTP 401). Give a token with `devpilot setup --github-token`")
    if status in (403, 429) and (headers.get("x-ratelimit-remaining") == "0" or headers.get("retry-after")):
        return RepoAccessError("rate_limited", f"GitHub's rate limit was reached (HTTP {status})" + ("" if has_token else "; a token raises the limit") + ". Try again later")
    if status == 403:
        sso = " The organization requires SAML single sign-on: authorize the token for it on GitHub." if headers.get("x-github-sso") else ""
        return RepoAccessError("forbidden", f"GitHub refused access to {what} (HTTP 403): the token lacks permission.{sso}")
    if status == 404:
        if has_token:
            return RepoAccessError("not_visible", f"{what} is not visible to this token (HTTP 404): check the name, or give the token read access to it (fine-grained: Contents: read)")
        return RepoAccessError("authorization_required", f"GitHub answered 404 for {what}. It may be private (GitHub answers 404 for private repositories without authorization) or the name may be wrong. Give a token with read access through `devpilot setup --github-token`, then try again")
    if status >= 500:
        return RepoAccessError("unreachable", f"GitHub had a server problem (HTTP {status}); try again shortly")
    return RepoAccessError("unreachable", f"GitHub answered HTTP {status} for {what}")


class GitHubRepoClient:
    def __init__(self, egress: EgressPolicy, *, token: str | None = None, client: httpx.Client | None = None, timeout: float = 30.0, max_text_bytes: int = 400_000) -> None:
        self._egress = egress
        self._token = token.strip() if token and token.strip() else None
        self._client = client or httpx.Client(timeout=timeout)
        self._max_text = max_text_bytes

    @property
    def authorized(self) -> bool:
        return self._token is not None

    def _get(self, url: str, *, accept: str = "application/vnd.github+json", max_bytes: int | None = None, what: str = "the repository") -> bytes:
        limit = max_bytes or self._max_text
        for _hop in range(_MAX_REDIRECTS + 1):
            target = httpx.URL(url)
            decision = self._egress.check("github", url)
            if not decision.allowed:
                raise RepoAccessError("refused", f"the network policy does not allow {target.host}: {decision.reason}")
            headers = {"Accept": accept, "User-Agent": "DevPilot-AI", "X-GitHub-Api-Version": "2022-11-28"}
            if self._token and target.scheme == "https" and target.host == API_HOST:
                headers["Authorization"] = f"Bearer {self._token}"
            try:
                with self._client.stream("GET", url, headers=headers, follow_redirects=False) as answer:
                    if answer.status_code in (301, 302, 303, 307, 308):
                        url = urljoin(url, answer.headers.get("location", ""))
                        continue
                    if answer.status_code != 200:
                        raise _status_problem(answer.status_code, answer.headers, has_token=self.authorized, what=what)
                    body = bytearray()
                    for chunk in answer.iter_bytes():
                        body += chunk
                        if len(body) > limit:
                            raise RepoAccessError("too_large", f"{what} is over the {limit:,}-byte limit")
                    return bytes(body)
            except httpx.TimeoutException:
                raise RepoAccessError("unreachable", f"GitHub ({target.host}) did not answer in time: check the internet connection or a proxy") from None
            except httpx.TransportError as exc:
                raise RepoAccessError("unreachable", f"GitHub ({target.host}) could not be reached ({type(exc).__name__}): check the internet connection, a VPN or a proxy") from None
        raise RepoAccessError("unreachable", "GitHub redirected too many times")

    def _json(self, path: str, *, what: str = "the repository") -> Any:
        try:
            return json.loads(self._get(f"{API}{path}", what=what))
        except ValueError:
            raise RepoAccessError("unreachable", f"GitHub's answer for {what} was not JSON") from None

    def info(self, repo: RepoRef) -> RepoInfo:
        data = self._json(f"/repos/{repo.full_name}", what=repo.full_name)
        lic = data.get("license") or {}
        return RepoInfo(
            full_name=str(data.get("full_name") or repo.full_name), url=str(data.get("html_url") or repo.url), private=bool(data.get("private")),
            default_branch=check_ref(str(data.get("default_branch") or "main")), archived=bool(data.get("archived")), size_kb=int(data.get("size") or 0),
            description=str(data.get("description") or "")[:500], license=lic.get("spdx_id") if isinstance(lic, dict) else None,
        )  # fmt: skip

    def resolve_commit(self, repo: RepoRef, ref: str) -> str:
        """The full SHA a branch, tag or SHA names now. An unknown ref is reported as such, with the ref."""
        check_ref(ref)
        try:
            sha = self._get(f"{API}/repos/{repo.full_name}/commits/{quote(ref, safe='')}", accept="application/vnd.github.sha", what=f"{repo.full_name} at {ref!r}").decode().strip()
        except RepoAccessError as exc:
            if exc.kind in ("not_visible",):
                raise RepoAccessError("ref_not_found", f"{repo.full_name} has no branch, tag or commit named {ref!r} that this token can see") from None
            raise
        if not COMMIT.fullmatch(sha):
            raise RepoAccessError("ref_not_found", f"{repo.full_name} has no branch, tag or commit named {ref!r}")
        return sha

    def tree(self, repo: RepoRef, commit: str) -> tuple[dict[str, int], bool]:
        data = self._json(f"/repos/{repo.full_name}/git/trees/{commit}?recursive=1", what=f"the file list of {repo.full_name}")
        files = {str(e["path"]): int(e.get("size") or 0) for e in data.get("tree", []) if e.get("type") == "blob" and isinstance(e.get("path"), str)}
        return files, bool(data.get("truncated"))

    def read_text(self, repo: RepoRef, commit: str, path: str, *, max_bytes: int = 200_000) -> str | None:
        """One file's text, or None when it is missing or too large. Untrusted: read as data only."""
        try:
            data = self._get(f"{API}/repos/{repo.full_name}/contents/{quote(path)}?ref={commit}", accept="application/vnd.github.raw", max_bytes=max_bytes, what=path)
        except RepoAccessError as exc:
            if exc.kind in ("not_visible", "authorization_required", "too_large"):
                return None
            raise
        return data.decode("utf-8", errors="replace")

    def archive(self, repo: RepoRef, commit: str, *, max_bytes: int) -> bytes:
        """The gzip tarball of exactly that commit. GitHub redirects to codeload.github.com, which is followed without the token."""
        if not COMMIT.fullmatch(commit):
            raise RepoAccessError("refused", "an archive is downloaded only for a full 40-character commit SHA")
        return self._get(f"{API}/repos/{repo.full_name}/tarball/{commit}", max_bytes=max_bytes, what=f"the archive of {repo.full_name}")


@dataclass(frozen=True)
class Checkout:
    """A repository read at one commit: what was asked for, what it resolved to, and its files (not yet written anywhere)."""

    repo: RepoRef
    info: RepoInfo
    commit: str
    files: dict[str, bytes]
    skipped: tuple[str, ...]


def checkout(client: GitHubRepoClient, repo: RepoRef, *, max_archive_bytes: int, max_files: int = 5_000, max_file_bytes: int = 5_000_000, max_total_bytes: int = 200_000_000) -> Checkout:
    """Resolve the ref, download that commit's archive and unpack it in memory, safely. Nothing is written or run."""
    from app.repos.importer import ImportLimits, ImportRefused, read_archive

    info = client.info(repo)
    commit = repo.pinned_commit or client.resolve_commit(repo, repo.ref or info.default_branch)
    data = client.archive(repo, commit, max_bytes=max_archive_bytes)
    try:
        files = read_archive(data, ImportLimits(max_archive_bytes, max_files, max_file_bytes, max_total_bytes))
    except ImportRefused as exc:
        raise RepoAccessError("refused", f"the repository was not imported: {exc}") from None
    return Checkout(repo, info, commit, files, ())
