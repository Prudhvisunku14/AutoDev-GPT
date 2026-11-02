"""GitHubSource with an in-process transport: no request leaves the machine. It has never been run against GitHub."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.egress import EgressPolicy
from app.repos.source import GitHubSource, RepositoryAccessRefused, RepositoryUnavailable

SHA = "a" * 40
REPO = {"full_name": "octo/weather", "html_url": "https://github.com/octo/weather", "description": "d", "language": "Python", "stargazers_count": 5,
        "license": {"spdx_id": "MIT"}, "archived": False, "pushed_at": "2026-10-01T00:00:00Z", "default_branch": "main", "size": 40, "topics": ["weather"]}  # fmt: skip


def github(*hosts: str) -> EgressPolicy:
    return EgressPolicy({"github": list(hosts)})


class Recorder:
    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes, self.seen = routes, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        key = f"{request.url.host}{request.url.raw_path.decode()}"
        return next((r for prefix, r in self.routes.items() if key.startswith(prefix)), httpx.Response(404))


def source(recorder: Recorder, *hosts: str) -> GitHubSource:
    return GitHubSource(github(*hosts), client=httpx.Client(transport=httpx.MockTransport(recorder)))


def test_nothing_is_sent_when_the_egress_policy_does_not_allow_github() -> None:
    recorder = Recorder({})
    with pytest.raises(RepositoryAccessRefused, match="egress policy refused api.github.com"):
        GitHubSource(EgressPolicy({"github": []}), client=httpx.Client(transport=httpx.MockTransport(recorder))).search("weather", limit=3)
    assert recorder.seen == []


def test_a_search_reads_public_data_and_sends_no_token() -> None:
    recorder = Recorder({"api.github.com/search/repositories": httpx.Response(200, json={"items": [REPO]})})
    found = source(recorder, "api.github.com").search("weather react", limit=3)
    assert [s.full_name for s in found] == ["octo/weather"] and found[0].license == "MIT"
    assert all("authorization" not in r.headers for r in recorder.seen)


def test_detail_pins_the_default_branchs_commit_and_reads_the_manifests() -> None:
    tree = {"tree": [{"path": "requirements.txt", "type": "blob", "size": 9}, {"path": "src/app.py", "type": "blob", "size": 20}], "truncated": False}
    recorder = Recorder({
        "api.github.com/repos/octo/weather/commits/main": httpx.Response(200, text=SHA), f"api.github.com/repos/octo/weather/git/trees/{SHA}": httpx.Response(200, json=tree),
        "api.github.com/repos/octo/weather/readme": httpx.Response(200, text="# readme"), "api.github.com/repos/octo/weather/license": httpx.Response(200, text="MIT License"),
        "api.github.com/repos/octo/weather/contents/requirements.txt": httpx.Response(200, text="fastapi\n"), "api.github.com/repos/octo/weather": httpx.Response(200, json=REPO),
    })  # fmt: skip
    detail = source(recorder, "api.github.com").detail("octo/weather")
    assert detail.commit == SHA and detail.manifests == {"requirements.txt": "fastapi\n"} and detail.license_text == "MIT License" and detail.source == "github"


def test_the_archive_redirect_to_codeload_is_refused_unless_it_is_allowlisted() -> None:
    routes = {f"api.github.com/repos/octo/weather/tarball/{SHA}": httpx.Response(302, headers={"location": f"https://codeload.github.com/octo/weather/legacy.tar.gz/{SHA}"}),
              "codeload.github.com/": httpx.Response(200, content=b"tarball")}  # fmt: skip
    refused = Recorder(routes)
    with pytest.raises(RepositoryAccessRefused, match="codeload.github.com"):
        source(refused, "api.github.com", "github.com").archive("octo/weather", SHA, max_bytes=1_000)
    assert [r.url.host for r in refused.seen] == ["api.github.com"]
    allowed = Recorder(routes)
    assert source(allowed, "api.github.com", "codeload.github.com").archive("octo/weather", SHA, max_bytes=1_000) == b"tarball"


def test_an_answer_over_its_limit_is_dropped() -> None:
    recorder = Recorder({f"api.github.com/repos/octo/weather/tarball/{SHA}": httpx.Response(200, content=b"x" * 2_000)})
    with pytest.raises(RepositoryUnavailable, match="limit"):
        source(recorder, "api.github.com").archive("octo/weather", SHA, max_bytes=1_000)


def test_a_missing_repository_is_unavailable() -> None:
    with pytest.raises(RepositoryUnavailable, match="404"):
        source(Recorder({}), "api.github.com").detail("octo/gone")


@pytest.mark.parametrize(("name", "commit"), [("../etc", SHA), ("octo/weather;rm", SHA), ("octo/weather", "main"), ("octo/weather", "A" * 40)])
def test_names_and_commits_are_checked_before_any_request(name: str, commit: str) -> None:
    recorder = Recorder({})
    with pytest.raises(RepositoryAccessRefused):
        source(recorder, "api.github.com").archive(name, commit, max_bytes=10)
    assert recorder.seen == []


def test_the_search_query_is_encoded_not_spliced() -> None:
    recorder = Recorder({"api.github.com/search/repositories": httpx.Response(200, json={"items": []})})
    source(recorder, "api.github.com").search("weather&per_page=100 react", limit=3)
    assert recorder.seen[0].url.params["per_page"] == "3" and json.dumps(recorder.seen[0].url.params["q"]).startswith('"weather&per_page=100')
