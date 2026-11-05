"""Writing to GitHub for the person, only after their approval: a new repository, a branch, one commit, a pull request.

Every call goes to api.github.com through the `github` egress entry, with the person's connection token in an Authorization
header. The writes are additive by construction:
- a pull request's commit goes on a **new branch** made from the base branch's current head, whose tree starts from the base
  tree, so files the commit does not name are kept as they are; the default branch itself is never written;
- an existing branch is never moved or force-updated: a name that is taken is refused;
- a new repository gets its first commit on its own default branch.
Nothing here decides *whether* to write: the PolicyEngine holds every call for a person's approval (GIT_PUSH).
**Tested only with an in-process transport; never run against GitHub here.**
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import EgressPolicy
from app.repos.github_url import check_ref

API = "https://api.github.com"
_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
MAX_FILES = 500
MAX_TOTAL_BYTES = 20_000_000


class GitHubWriteError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # unauthorized | forbidden | not_found | conflict | invalid | unreachable


@dataclass(frozen=True)
class Published:
    repository: str
    branch: str
    commit: str
    files: int
    pull_request_url: str | None
    repository_url: str

    def to_dict(self) -> dict[str, object]:
        return {"repository": self.repository, "branch": self.branch, "commit": self.commit, "files": self.files,
                "pull_request_url": self.pull_request_url, "repository_url": self.repository_url}  # fmt: skip


def check_repo_name(name: str) -> str:
    if not _REPO_NAME.fullmatch(name) or name in (".", "..") or name.endswith(".git"):
        raise GitHubWriteError("invalid", f"{name!r} is not a valid GitHub repository name")
    return name


class GitHubWriter:
    def __init__(self, egress: EgressPolicy, token: str, *, client: httpx.Client | None = None) -> None:
        if not token:
            raise GitHubWriteError("unauthorized", "connect GitHub first: no token is available")
        self._egress = egress
        self._token = token
        self._client = client or httpx.Client(timeout=30.0, headers={"User-Agent": "DevPilot-AI"}, follow_redirects=False)

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None, *, ok: tuple[int, ...] = (200, 201)) -> dict[str, Any]:
        url = f"{API}{path}"
        if not self._egress.check("github", url).allowed:
            raise GitHubWriteError("unreachable", "the egress allowlist does not allow api.github.com for the github component")
        try:
            response = self._client.request(method, url, json=body, headers={"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                                                                              "X-GitHub-Api-Version": "2022-11-28"})  # fmt: skip
        except httpx.HTTPError as exc:
            raise GitHubWriteError("unreachable", f"GitHub could not be reached: {type(exc).__name__}") from None
        if response.status_code in ok:
            try:
                data = response.json()
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {"items": data}
        message = ""
        try:
            message = str(response.json().get("message", ""))[:200]
        except (ValueError, AttributeError):
            pass
        kind = {401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict", 422: "invalid"}.get(response.status_code, "unreachable")
        raise GitHubWriteError(kind, f"GitHub answered HTTP {response.status_code} to {method} {path.split('?')[0]}" + (f": {message}" if message else ""))

    # --- Reads ------------------------------------------------------------------------------------------------------

    def login(self) -> str:
        return str(self._call("GET", "/user").get("login", ""))

    def repository(self, full_name: str) -> dict[str, Any]:
        return self._call("GET", f"/repos/{full_name}")

    def _head(self, full_name: str, branch: str) -> str:
        ref = self._call("GET", f"/repos/{full_name}/git/ref/heads/{quote(branch, safe='/')}")
        sha = ref.get("object", {}).get("sha")
        if not isinstance(sha, str):
            raise GitHubWriteError("not_found", f"{full_name} has no branch {branch!r}")
        return sha

    def _branch_exists(self, full_name: str, branch: str) -> bool:
        try:
            self._head(full_name, branch)
        except GitHubWriteError as exc:
            if exc.kind == "not_found":
                return False
            raise
        return True

    # --- Writes -----------------------------------------------------------------------------------------------------

    def _commit(self, full_name: str, parent: str, files: dict[str, bytes], message: str) -> str:
        if not files or len(files) > MAX_FILES or sum(len(v) for v in files.values()) > MAX_TOTAL_BYTES:
            raise GitHubWriteError("invalid", f"a commit holds 1 to {MAX_FILES} files and at most {MAX_TOTAL_BYTES // 1_000_000} MB")
        base_tree = self._call("GET", f"/repos/{full_name}/git/commits/{parent}").get("tree", {}).get("sha")
        entries = []
        for path, content in sorted(files.items()):
            blob = self._call("POST", f"/repos/{full_name}/git/blobs", {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = self._call("POST", f"/repos/{full_name}/git/trees", {"base_tree": base_tree, "tree": entries})  # base_tree keeps every other file
        commit = self._call("POST", f"/repos/{full_name}/git/commits", {"message": message, "tree": tree["sha"], "parents": [parent]})
        return str(commit["sha"])

    def checked_repository(self, full_name: str) -> dict[str, Any]:
        """The repository, refused unless GitHub confirms it is exactly the one named and that this account may push to it."""
        repo = self.repository(full_name)
        if str(repo.get("full_name", "")).lower() != full_name.lower():
            raise GitHubWriteError("invalid", "GitHub answered for a repository other than the one requested")
        permissions = repo.get("permissions")
        if isinstance(permissions, dict) and not permissions.get("push", False):
            raise GitHubWriteError("forbidden", f"this GitHub account cannot push to {full_name}")
        return repo

    def open_pull_request(self, full_name: str, *, files: dict[str, bytes], branch: str, base: str | None, title: str, body: str, message: str,
                          draft: bool = False) -> Published:  # fmt: skip
        """The commit is made before the branch exists, so a failed upload never leaves a half-made branch behind."""
        check_ref(branch)
        repo = self.checked_repository(full_name)
        base_branch = check_ref(base or str(repo.get("default_branch") or "main"))
        if branch == base_branch:
            raise GitHubWriteError("invalid", "the pull request branch must differ from its base branch")
        if self._branch_exists(full_name, branch):
            raise GitHubWriteError("conflict", f"{full_name} already has a branch {branch!r}; DevPilot never moves an existing branch")
        parent = self._head(full_name, base_branch)
        commit = self._commit(full_name, parent, files, message)
        self._call("POST", f"/repos/{full_name}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
        pr = self._call("POST", f"/repos/{full_name}/pulls", {"title": title, "head": branch, "base": base_branch, "body": body, "maintainer_can_modify": True, "draft": draft})
        return Published(full_name, branch, commit, len(files), str(pr.get("html_url") or ""), str(repo.get("html_url") or f"https://github.com/{full_name}"))

    def create_repository(self, name: str, *, private: bool, description: str, files: dict[str, bytes], message: str) -> Published:
        check_repo_name(name)
        repo = self._call("POST", "/user/repos", {"name": name, "private": private, "description": description[:300], "auto_init": True})
        full_name = str(repo["full_name"])
        branch = check_ref(str(repo.get("default_branch") or "main"))
        parent = self._head(full_name, branch)
        commit = self._commit(full_name, parent, files, message)
        self._call("PATCH", f"/repos/{full_name}/git/refs/heads/{quote(branch, safe='/')}", {"sha": commit, "force": False})  # a fast-forward from the initial commit
        return Published(full_name, branch, commit, len(files), None, str(repo.get("html_url") or f"https://github.com/{full_name}"))


def describe_files(files: dict[str, bytes]) -> list[dict[str, object]]:
    return [{"path": path, "bytes": len(content)} for path, content in sorted(files.items())]
