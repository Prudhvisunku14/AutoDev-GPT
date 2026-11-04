"""GitHub repositories named by a person: URL validation, authorization-aware errors, token handling and a safe checkout.

GitHub is an in-memory httpx.MockTransport. No network call is made; the tokens are fake.
"""

from __future__ import annotations

import io
import json
import tarfile
from typing import Any

import httpx
import pytest

from app.core.egress import EgressPolicy
from app.repos.github_access import GitHubRepoClient, RepoAccessError, checkout
from app.repos.github_url import RepoRef, RepoUrlError, find_repo_url, parse_repo_url

TOKEN = "github_pat_" + "T" * 60
SHA = "a" * 40
EGRESS = EgressPolicy({"github": ["github.com", "api.github.com", "codeload.github.com"]})


# --- URLs ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("url", "full", "ref"), [
    ("https://github.com/Owner-1/my.repo", "Owner-1/my.repo", None),
    ("https://github.com/owner/site.git", "owner/site", None),
    ("https://www.github.com/owner/site/", "owner/site", None),
    ("https://github.com/owner/site/tree/feature/new-home", "owner/site", "feature/new-home"),
    ("https://github.com/owner/site/commit/" + SHA, "owner/site", SHA),
    ("git@github.com:owner/site.git", "owner/site", None),
])  # fmt: skip
def test_accepted_forms_are_normalised(url: str, full: str, ref: str | None) -> None:
    repo = parse_repo_url(url)
    assert (repo.full_name, repo.ref, repo.url) == (full, ref, f"https://github.com/{full}")


@pytest.mark.parametrize(("url", "why"), [
    ("http://github.com/owner/site", "only https"),
    ("https://user:secret@github.com/owner/site", "credentials"),
    ("https://ghp_abc@github.com/owner/site", "credentials"),
    ("https://github.com.evil.example/owner/site", "only github.com"),
    ("https://gitlab.com/owner/site", "only github.com"),
    ("https://github.com:8443/owner/site", "port"),
    ("https://github.com/owner/site?token=x", "query"),
    ("https://github.com/owner/site#readme", "query or fragment"),
    ("https://github.com/owner/%2e%2e/site", "percent"),
    ("https://github.com/owner/../site", "'..'"),
    ("https://github.com/owner", "owner and a repository"),
    ("https://github.com/owner/site/blob/main/README.md", "only the repository"),
    ("https://github.com/owner/site/tree/..%2Fx", "percent"),
    ("https://github.com/owner/site/tree/a..b", "not a valid branch"),
    ("https://github.com/owner/site/tree/-rf", "not a valid branch"),
    ("https://github.com/-owner/site", "not a valid owner"),
    ("https://github.com/own er/site", "spaces"),
    ("https://github.com/owner/sité", "non-ASCII"),
])  # fmt: skip
def test_unsafe_or_unsupported_urls_are_refused_with_the_reason(url: str, why: str) -> None:
    with pytest.raises(RepoUrlError, match=why):
        parse_repo_url(url)


def test_a_refused_url_with_credentials_is_not_echoed() -> None:
    with pytest.raises(RepoUrlError) as caught:
        parse_repo_url("https://me:hunter2@github.com/o/r")
    assert "hunter2" not in str(caught.value)


def test_a_ref_option_overrides_the_url_and_is_checked() -> None:
    assert parse_repo_url("https://github.com/o/r/tree/main", ref="v2.0").ref == "v2.0"
    with pytest.raises(RepoUrlError):
        parse_repo_url("https://github.com/o/r", ref="bad..ref")


def test_a_repository_is_found_in_free_text_with_trailing_punctuation() -> None:
    repo = find_repo_url("Build the Acme website using https://github.com/some-org/some-site.git.")
    assert repo == RepoRef("some-org", "some-site")
    assert find_repo_url("start from github.com/o/r, please") == RepoRef("o", "r")
    assert find_repo_url("a csv to json converter in python") is None


def test_free_text_naming_two_repositories_or_another_host_is_refused() -> None:
    with pytest.raises(RepoUrlError, match="more than one"):
        find_repo_url("merge https://github.com/a/one and https://github.com/b/two")
    with pytest.raises(RepoUrlError, match="gitlab.com"):
        find_repo_url("use https://gitlab.com/a/site please")
    with pytest.raises(RepoUrlError, match="only https"):
        find_repo_url("use http://github.com/a/site")


# --- the client ---------------------------------------------------------------------------------------------


class GitHub:
    """A tiny GitHub: routes by path, records what was sent."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        key = f"{request.url.host}{request.url.path}"
        answer = self.routes.get(key)
        if answer is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if callable(answer):
            return answer(request)
        status, body, headers = answer if isinstance(answer, tuple) else (200, answer, {})
        content = body if isinstance(body, bytes) else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
        return httpx.Response(status, content=content, headers=headers)


def client(gh: GitHub, token: str | None = None, **kw: Any) -> GitHubRepoClient:
    return GitHubRepoClient(EGRESS, token=token, client=httpx.Client(transport=httpx.MockTransport(gh)), **kw)


REPO = RepoRef("o", "site")
INFO = {"full_name": "o/site", "html_url": "https://github.com/o/site", "private": True, "default_branch": "main", "size": 12, "license": {"spdx_id": "MIT"}}


def test_without_a_token_a_404_is_reported_as_possibly_private_never_as_missing() -> None:
    with pytest.raises(RepoAccessError) as caught:
        client(GitHub({})).info(REPO)
    exc = caught.value
    assert exc.kind == "authorization_required" and exc.needs_authorization
    assert "may be private" in str(exc) and "does not exist" not in str(exc) and "setup --github-token" in str(exc)


def test_with_a_token_a_404_means_not_visible_to_that_token() -> None:
    with pytest.raises(RepoAccessError) as caught:
        client(GitHub({}), TOKEN).info(REPO)
    assert caught.value.kind == "not_visible" and "not visible to this token" in str(caught.value) and TOKEN not in str(caught.value)


@pytest.mark.parametrize(("status", "headers", "token", "kind"), [
    (401, {}, TOKEN, "token_rejected"),
    (401, {}, None, "authorization_required"),
    (403, {"x-ratelimit-remaining": "0"}, None, "rate_limited"),
    (429, {"retry-after": "60"}, TOKEN, "rate_limited"),
    (403, {}, TOKEN, "forbidden"),
    (502, {}, TOKEN, "unreachable"),
])  # fmt: skip
def test_each_github_answer_has_its_own_meaning(status: int, headers: dict[str, str], token: str | None, kind: str) -> None:
    with pytest.raises(RepoAccessError) as caught:
        client(GitHub({"api.github.com/repos/o/site": (status, {"message": "x"}, headers)}), token).info(REPO)
    assert caught.value.kind == kind and f"{status}" in str(caught.value)


def test_sso_enforcement_is_named() -> None:
    with pytest.raises(RepoAccessError, match="single sign-on"):
        client(GitHub({"api.github.com/repos/o/site": (403, {}, {"x-github-sso": "required; url=https://github.com/orgs/o/sso"})}), TOKEN).info(REPO)


def test_the_token_goes_only_to_the_api_as_a_header_and_never_to_codeload() -> None:
    archive = _tarball({"index.html": b"<h1>x</h1>"})
    signed = "https://codeload.github.com/o/site/legacy.tar.gz/" + SHA + "?token=SIGNED-SECRET"
    gh = GitHub({f"api.github.com/repos/o/site/tarball/{SHA}": (302, b"", {"location": signed}), f"codeload.github.com/o/site/legacy.tar.gz/{SHA}": (200, archive, {})})
    assert client(gh, TOKEN).archive(REPO, SHA, max_bytes=10_000_000) == archive
    api, code = gh.seen
    assert api.headers["authorization"] == f"Bearer {TOKEN}" and TOKEN not in str(api.url)
    assert "authorization" not in code.headers


def test_a_redirect_outside_the_allowlist_is_refused_before_it_is_followed() -> None:
    gh = GitHub({f"api.github.com/repos/o/site/tarball/{SHA}": (302, b"", {"location": "https://evil.example/steal"})})
    with pytest.raises(RepoAccessError) as caught:
        client(gh, TOKEN).archive(REPO, SHA, max_bytes=1_000)
    assert caught.value.kind == "refused" and len(gh.seen) == 1


def test_an_error_on_the_signed_download_never_shows_its_url() -> None:
    signed = "https://codeload.github.com/o/site/legacy.tar.gz/" + SHA + "?token=SIGNED-SECRET"
    gh = GitHub({f"api.github.com/repos/o/site/tarball/{SHA}": (302, b"", {"location": signed}), f"codeload.github.com/o/site/legacy.tar.gz/{SHA}": (500, b"", {})})
    with pytest.raises(RepoAccessError) as caught:
        client(gh, TOKEN).archive(REPO, SHA, max_bytes=1_000)
    assert "SIGNED-SECRET" not in str(caught.value)


def test_answers_have_a_size_limit() -> None:
    gh = GitHub({"api.github.com/repos/o/site": (200, b"x" * 2_000, {})})
    with pytest.raises(RepoAccessError) as caught:
        client(gh, max_text_bytes=1_000).info(REPO)
    assert caught.value.kind == "too_large"


def test_a_dead_network_is_reported_as_such() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(RepoAccessError) as caught:
        GitHubRepoClient(EGRESS, client=httpx.Client(transport=httpx.MockTransport(down))).info(REPO)
    assert caught.value.kind == "unreachable" and "internet" in str(caught.value)


def test_an_unknown_branch_is_named() -> None:
    with pytest.raises(RepoAccessError) as caught:
        client(GitHub({}), TOKEN).resolve_commit(REPO, "no-such-branch")
    assert caught.value.kind == "ref_not_found" and "no-such-branch" in str(caught.value)


def _tarball(files: dict[str, bytes], *, link: bool = False) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"o-site-{SHA[:7]}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(f"o-site-{SHA[:7]}/evil")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(info)
    return buf.getvalue()


def _repo_routes(archive: bytes, *, branch: str = "main") -> dict[str, Any]:
    return {"api.github.com/repos/o/site": INFO, f"api.github.com/repos/o/site/commits/{branch}": (200, SHA, {}), f"api.github.com/repos/o/site/tarball/{SHA}": (200, archive, {})}


def test_a_checkout_resolves_the_default_branch_to_a_commit_and_unpacks_it_in_memory() -> None:
    gh = GitHub(_repo_routes(_tarball({"index.html": b"<h1>Home</h1>", "css/site.css": b"h1{}"})))
    got = checkout(client(gh, TOKEN), REPO, max_archive_bytes=10_000_000)
    assert got.commit == SHA and got.info.private and got.files == {"index.html": b"<h1>Home</h1>", "css/site.css": b"h1{}"}


def test_a_checkout_of_a_named_branch_uses_that_branch() -> None:
    gh = GitHub(_repo_routes(_tarball({"a.txt": b"1"}), branch="redesign"))
    assert checkout(client(gh, TOKEN), REPO.with_ref("redesign"), max_archive_bytes=10_000_000).commit == SHA


def test_an_archive_with_a_link_is_refused_whole() -> None:
    gh = GitHub(_repo_routes(_tarball({"index.html": b"x"}, link=True)))
    with pytest.raises(RepoAccessError) as caught:
        checkout(client(gh, TOKEN), REPO, max_archive_bytes=10_000_000)
    assert caught.value.kind == "refused" and "links are not imported" in str(caught.value)
