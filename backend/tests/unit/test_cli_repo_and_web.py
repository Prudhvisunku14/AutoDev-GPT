"""The CLI with GitHub repositories and websites: authorization first, the repository decides the stack, nothing is spent on
what cannot be verified, an accepted repository is imported before the run. GitHub and Docker are fakes; GPT is scripted."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.cli import build, github_token, inspect_cmd, setup as cli_setup, sources, verify_site, webruntime
from app.cli.paths import Paths
from app.repos.github_access import Checkout, RepoAccessError, RepoInfo
from app.repos.github_url import RepoRef
from app.repos.inspect import inspect_project
from app.verification import web
from tests.unit.test_cli_build import CONFIG, PROMPT, ScriptedIO, SimulatedEngine, _fast_and_clean, command, p, ready  # noqa: F401 - fixtures

SHA = "b" * 40
SITE_FILES = {"package.json": '{"dependencies":{"react":"18.3.1","react-dom":"18.3.1"},"devDependencies":{"vite":"5.4.11","typescript":"5.6.3"}}', "index.html": "<div id=root>", "src/App.tsx": ""}
CLI_FILES = {"tool.py": "print('hi')\n", "requirements.txt": "\n", "README.md": "# Tool"}


def remote(files: dict[str, str], *, private: bool = True) -> sources.RemoteProject:
    info = RepoInfo("acme/thing", "https://github.com/acme/thing", private, "main", False, 4, "", "MIT")
    return sources.RemoteProject(RepoRef("acme", "thing"), info, SHA, inspect_project({k: len(v) for k, v in files.items()}, files.get))


def checkout_of(project: sources.RemoteProject, files: dict[str, str]) -> Checkout:
    return Checkout(project.repo, project.info, SHA, {k: v.encode() for k, v in files.items()}, ())


@pytest.fixture(autouse=True)
def _no_web_approval() -> Any:
    web._APPROVED.clear()
    yield
    web._APPROVED.clear()


def test_an_inaccessible_repository_asks_for_authorization_and_spends_nothing(ready: list[str], p: Paths) -> None:
    said: list[str] = []

    def denied(paths: Paths, repo: RepoRef, say: Any) -> None:
        said.append(str(repo))
        say(sources.explain(RepoAccessError("authorization_required", "GitHub answered 404 for acme/thing. It may be private"), has_token=False))
        return None

    io = ScriptedIO()
    code = build.build_command(p, CONFIG, prompt="Build the Studio website using https://github.com/acme/thing.git", stack=None, max_usd=None, output=None,
                               approve_spend=None, io=io, interactive=True, look=denied)  # fmt: skip
    assert code == 2 and said == ["https://github.com/acme/thing"] and ready == [] and io.asked == []
    assert "setup --github-token" in io.text and "Contents: Read-only" in io.text and "Nothing was started or spent" in io.text


def test_a_website_repository_is_inspected_and_declined_with_the_reason_before_any_approval(ready: list[str], p: Paths) -> None:
    site = remote(SITE_FILES)
    io = ScriptedIO()
    code = build.build_command(p, CONFIG, prompt="Build the Studio website using https://github.com/acme/thing", stack=None, max_usd=None, output=None,
                               approve_spend=None, io=io, interactive=True, look=lambda paths, repo, say: (object(), site))  # fmt: skip
    assert code == 2 and ready == [] and io.asked == []  # no approval was asked for, no engine was made
    assert "React + Vite + TypeScript single-page application" in io.text and "Not built: a React + Vite + TypeScript website cannot be built yet" in io.text
    assert "verify-site" in io.text and "Nothing was started or spent" in io.text


def test_a_branch_option_is_carried_to_github(ready: list[str], p: Paths) -> None:
    seen: list[RepoRef] = []
    build.build_command(p, CONFIG, prompt="Build the Studio website using https://github.com/acme/thing", stack=None, max_usd=None, output=None, approve_spend=None,
                        io=ScriptedIO(), interactive=True, ref="redesign", look=lambda paths, repo, say: seen.append(repo))  # fmt: skip
    assert seen == [RepoRef("acme", "thing", "redesign")]
    io = ScriptedIO()
    assert build.build_command(p, CONFIG, prompt=PROMPT, stack=None, max_usd=None, output=None, approve_spend=None, io=io, interactive=True, ref="main") == 2
    assert "names no GitHub repository" in io.text


def test_an_accepted_python_repository_is_imported_into_the_project_before_the_run(ready: list[str], p: Paths, tmp_path: Path) -> None:
    project = remote(CLI_FILES)
    fetched: list[str] = []

    def fetch(gh: Any, rp: sources.RemoteProject) -> Checkout:
        fetched.append(rp.commit)
        return checkout_of(rp, CLI_FILES)

    io = ScriptedIO("approve 0.50")
    build.build_command(p, CONFIG, prompt="Add a --verbose flag to this command-line tool https://github.com/acme/thing", stack=None, max_usd=None, output=tmp_path / "out",
                        approve_spend=None, io=io, interactive=True, look=lambda paths, repo, say: (object(), project), fetch=fetch, engine_factory=build._default_engine)  # fmt: skip
    assert fetched == [SHA] and ready == ["0.50"]
    assert "Stack: python-cli" in io.text and "starting from the repository (modify)" in io.text and f"Imported 3 file(s) from acme/thing at {SHA[:12]}" in io.text
    engine = SimulatedEngine.instances[0]
    project_row = engine.client.get("/api/v1/projects", headers={"Authorization": f"Bearer {engine.settings.devpilot_api_token.get_secret_value()}"}).json()[0]
    assert f"at commit {SHA}" in project_row["requirement_text"] and "tool.py" in project_row["requirement_text"]
    assert "print('hi')" not in project_row["requirement_text"]  # file names, never repository contents, reach the planner
    fs = engine.app.state.workspace_fs
    assert {"tool.py", "THIRD_PARTY_NOTICES.md", "DEVPILOT_IMPORT.json"} <= set(fs.list_files(project_row["id"]))


def test_the_source_context_fits_the_api_limit_however_large_the_repository() -> None:
    big = checkout_of(remote(CLI_FILES), {f"pkg/module_{i:05}.py": "" for i in range(5_000)})
    text = build.source_context(big, "adapt", room=3_000)
    assert len(text) <= 3_000 and "more" in text


def test_inspect_reports_the_stack_and_whether_builds_are_accepted(monkeypatch: pytest.MonkeyPatch, p: Paths) -> None:
    monkeypatch.setattr(sources, "access", lambda paths, repo, say: (object(), remote(SITE_FILES)))
    io = ScriptedIO()
    assert inspect_cmd.inspect_command(p, "https://github.com/acme/thing", io=io) == 0
    assert "Stack: react-vite" in io.text and "not accepted yet" in io.text and "acme/thing" in io.text
    assert inspect_cmd.inspect_command(p, "https://gitlab.com/acme/thing", io=ScriptedIO()) == 2


def test_verify_site_checks_a_local_folder_with_the_approved_verifier_and_saves_a_report(tmp_path: Path, p: Paths) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>x</h1>")

    class Verifier:
        def __init__(self) -> None:
            self.kinds: list[str] = []

        def image_id(self) -> str:
            return "sha256:" + "e" * 64

        def verify(self, files: Any, kind: str, **kw: Any) -> web.WebResult:
            self.kinds.append(kind)
            return web.WebResult("PASSED", "every stage passed, including a real browser", kind, (web.Stage("browser", True, "1 page"),))

    v = Verifier()
    io = ScriptedIO()
    assert verify_site.verify_site_command(p, str(site), output=tmp_path / "out", io=io, verifier=v) == 1  # type: ignore[arg-type]
    assert "devpilot setup --web" in io.text and v.kinds == []  # not self-tested here: refused
    webruntime.approve(p, v.image_id(), [])
    io = ScriptedIO()
    assert verify_site.verify_site_command(p, str(site), output=tmp_path / "out", io=io, verifier=v) == 0  # type: ignore[arg-type]
    assert v.kinds == ["static-html"] and list((tmp_path / "out").glob("verify-site-*.json"))


def test_verify_site_refuses_a_project_it_cannot_check(tmp_path: Path, p: Paths) -> None:
    folder = tmp_path / "next"
    folder.mkdir()
    (folder / "package.json").write_text('{"dependencies":{"next":"14","react":"18"}}')
    io = ScriptedIO()
    assert verify_site.verify_site_command(p, str(folder), io=io) == 2 and "Next.js" in io.text


def test_a_github_token_is_stored_hidden_and_encrypted_and_env_wins(monkeypatch: pytest.MonkeyPatch, p: Paths) -> None:
    token = "github_pat_" + "Q" * 60
    stored: list[str] = []
    monkeypatch.setattr(github_token, "store", lambda path, value: stored.append(value))
    io = ScriptedIO()
    assert cli_setup.setup_extras(p, web_runtime=False, github=True, io=io, ask_secret=lambda prompt: token) == 0  # type: ignore[arg-type]
    assert stored == [token] and token not in io.text and "Contents: Read-only" in io.text
    assert cli_setup.setup_extras(p, web_runtime=False, github=True, interactive=False, io=ScriptedIO()) == 1  # type: ignore[arg-type]
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    assert github_token.load(p.github_credentials) == "from-env" and github_token.source(p.github_credentials) == "the GITHUB_TOKEN environment variable"
    assert github_token.plausible(token) and not github_token.plausible("ghp_short") and not github_token.plausible("sk-" + "x" * 40)
