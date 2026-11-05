"""An in-process GitHub for tests: OAuth's code exchange, the REST reads DevPilot makes, and the Git Data API writes.

It keeps real Git semantics where DevPilot depends on them: a tree built with `base_tree` keeps every file it does not name,
refs move only when told, a taken branch name is refused, and a private repository answers 404 without a token. Every
request is recorded, so a test can check where a token was (and was not) sent.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

TOKEN = "gho_" + "T" * 36
CLIENT_ID = "Iv1.testclient0000"
CLIENT_SECRET = "test-github-client-secret-" + "c" * 20


def _sha(*parts: object) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class Repo:
    full_name: str
    private: bool = False
    default_branch: str = "main"
    license: str | None = "MIT"
    branches: dict[str, str] = field(default_factory=dict)
    pulls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FakeGitHub:
    repos: dict[str, Repo] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    trees: dict[str, dict[str, str]] = field(default_factory=dict)  # tree sha -> {path: blob sha}
    commits: dict[str, dict[str, Any]] = field(default_factory=dict)  # commit sha -> {tree, parents, message}
    codes: dict[str, str] = field(default_factory=dict)  # code -> expected PKCE verifier ("" accepts any)
    granted_scope: str = "repo"
    login: str = "octo-person"
    token: str = TOKEN
    revoked: list[str] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    exchanges: list[dict[str, Any]] = field(default_factory=list)

    # --- Building repositories ------------------------------------------------------------------------------------

    def add_repo(self, full_name: str, files: dict[str, str], *, private: bool = False, license: str | None = "MIT") -> Repo:
        tree = self._tree({path: self._blob(text.encode()) for path, text in files.items()})
        commit = self._commit(tree, [], "initial")
        repo = Repo(full_name, private, license=license, branches={"main": commit})
        self.repos[full_name.lower()] = repo
        return repo

    def files_at(self, full_name: str, branch: str) -> dict[str, bytes]:
        repo = self.repos[full_name.lower()]
        tree = self.trees[self.commits[repo.branches[branch]]["tree"]]
        return {path: self.blobs[sha] for path, sha in tree.items()}

    def _blob(self, content: bytes) -> str:
        sha = _sha("blob", content.hex())
        self.blobs[sha] = content
        return sha

    def _tree(self, entries: dict[str, str]) -> str:
        sha = _sha("tree", sorted(entries.items()))
        self.trees[sha] = dict(entries)
        return sha

    def _commit(self, tree: str, parents: list[str], message: str) -> str:
        sha = _sha("commit", tree, parents, message, len(self.commits))
        self.commits[sha] = {"tree": tree, "parents": parents, "message": message}
        return sha

    # --- Transport -------------------------------------------------------------------------------------------------

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def authorized(self, request: httpx.Request) -> bool:
        return request.headers.get("authorization") == f"Bearer {self.token}" and self.token not in self.revoked

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = urlsplit(str(request.url))
        if url.hostname == "github.com" and url.path == "/login/oauth/access_token":
            return self._exchange(request)
        if url.hostname != "api.github.com":
            return httpx.Response(404, json={"message": "Not Found"})
        path = unquote(url.path)
        if path == "/user":
            return httpx.Response(200, json={"login": self.login}) if self.authorized(request) else httpx.Response(401, json={"message": "Bad credentials"})
        if path.startswith("/applications/") and path.endswith("/grant") and request.method == "DELETE":
            body = json.loads(request.content or b"{}")
            self.revoked.append(body.get("access_token", ""))
            return httpx.Response(204)
        if path == "/user/repos" and request.method == "POST":
            return self._create_repo(request)
        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "repos":
            return httpx.Response(404, json={"message": "Not Found"})
        repo = self.repos.get(f"{parts[1]}/{parts[2]}".lower())
        if repo is None or (repo.private and not self.authorized(request)):
            return httpx.Response(404, json={"message": "Not Found"})
        rest = parts[3:]
        return self._repo_call(request, repo, rest, parse_qs(url.query))

    def _exchange(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.exchanges.append(body)
        expected = self.codes.pop(body.get("code", ""), None)
        if expected is None or body.get("client_secret") != CLIENT_SECRET or (expected and expected != body.get("code_verifier")):
            return httpx.Response(200, json={"error": "bad_verification_code"})
        return httpx.Response(200, json={"access_token": self.token, "token_type": "bearer", "scope": self.granted_scope})

    def _create_repo(self, request: httpx.Request) -> httpx.Response:
        if not self.authorized(request):
            return httpx.Response(401, json={"message": "Bad credentials"})
        body = json.loads(request.content)
        full_name = f"{self.login}/{body['name']}"
        if full_name.lower() in self.repos:
            return httpx.Response(422, json={"message": "name already exists on this account"})
        repo = self.add_repo(full_name, {"README.md": f"# {body['name']}\n"}, private=bool(body.get("private")), license=None)
        return httpx.Response(201, json={"full_name": repo.full_name, "default_branch": "main", "html_url": f"https://github.com/{repo.full_name}", "private": repo.private})

    def _repo_call(self, request: httpx.Request, repo: Repo, rest: list[str], query: dict[str, list[str]]) -> httpx.Response:
        method = request.method
        write = method in ("POST", "PATCH", "DELETE")
        if write and not self.authorized(request):
            return httpx.Response(401 if not request.headers.get("authorization") else 403, json={"message": "Resource not accessible"})
        if not rest and method == "GET":
            return httpx.Response(200, json={"full_name": repo.full_name, "html_url": f"https://github.com/{repo.full_name}", "private": repo.private,
                                             "default_branch": repo.default_branch, "archived": False, "size": 10, "description": "a test repository",
                                             "license": {"spdx_id": repo.license} if repo.license else None})  # fmt: skip
        if rest[:1] == ["commits"] and method == "GET":
            ref = "/".join(rest[1:])
            sha = repo.branches.get(ref) or (ref if ref in self.commits else None)
            return httpx.Response(200, text=sha) if sha else httpx.Response(422, json={"message": "No commit found"})
        if rest[:2] == ["git", "trees"] and method == "GET":
            commit = self.commits.get(rest[2])
            if commit is None:
                return httpx.Response(404, json={"message": "Not Found"})
            tree = self.trees[commit["tree"]]
            return httpx.Response(200, json={"truncated": False, "tree": [{"path": p, "type": "blob", "size": len(self.blobs[s])} for p, s in sorted(tree.items())]})
        if rest[:1] == ["contents"] and method == "GET":
            commit = self.commits.get((query.get("ref") or [""])[0])
            tree = self.trees[commit["tree"]] if commit else {}
            sha = tree.get("/".join(rest[1:]))
            return httpx.Response(200, content=self.blobs[sha]) if sha else httpx.Response(404, json={"message": "Not Found"})
        if rest[:3] == ["git", "ref", "heads"] and method == "GET":
            sha = repo.branches.get("/".join(rest[3:]))
            return httpx.Response(200, json={"object": {"sha": sha}}) if sha else httpx.Response(404, json={"message": "Not Found"})
        if rest[:2] == ["git", "commits"] and method == "GET":
            commit = self.commits.get(rest[2])
            return httpx.Response(200, json={"sha": rest[2], "tree": {"sha": commit["tree"]}}) if commit else httpx.Response(404, json={"message": "Not Found"})
        body = json.loads(request.content or b"{}")
        if rest == ["git", "blobs"] and method == "POST":
            return httpx.Response(201, json={"sha": self._blob(base64.b64decode(body["content"]))})
        if rest == ["git", "trees"] and method == "POST":
            entries = dict(self.trees.get(body.get("base_tree") or "", {}))  # Git keeps every file the new tree does not name
            entries.update({e["path"]: e["sha"] for e in body["tree"]})
            return httpx.Response(201, json={"sha": self._tree(entries)})
        if rest == ["git", "commits"] and method == "POST":
            return httpx.Response(201, json={"sha": self._commit(body["tree"], body["parents"], body["message"])})
        if rest == ["git", "refs"] and method == "POST":
            name = body["ref"].removeprefix("refs/heads/")
            if name in repo.branches:
                return httpx.Response(422, json={"message": "Reference already exists"})
            repo.branches[name] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"]})
        if rest[:3] == ["git", "refs", "heads"] and method == "PATCH":
            name = "/".join(rest[3:])
            old = repo.branches.get(name)
            if old is None or (not body.get("force") and old not in self.commits[body["sha"]]["parents"]):
                return httpx.Response(422, json={"message": "Update is not a fast forward"})
            repo.branches[name] = body["sha"]
            return httpx.Response(200, json={"ref": f"refs/heads/{name}"})
        if rest == ["pulls"] and method == "POST":
            if body["head"] not in repo.branches or body["base"] not in repo.branches:
                return httpx.Response(422, json={"message": "Validation Failed"})
            number = len(repo.pulls) + 1
            repo.pulls.append({**body, "number": number})
            return httpx.Response(201, json={"number": number, "html_url": f"https://github.com/{repo.full_name}/pull/{number}"})
        return httpx.Response(404, json={"message": "Not Found"})
