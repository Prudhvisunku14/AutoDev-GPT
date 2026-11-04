"""Offline integration: real verification/orchestration code, scripted Docker replies.

These tests do not start Chromium, install dependencies, or generate a website.
"""
from dataclasses import replace
import json
import subprocess
import threading

import pytest

from app.db.models import Project, Run, RunState
from app.sandbox.fake import FakeSandbox
from app.sandbox.spec import ExecStatus
from app.tools.adapters import coding
from app.tools.base import ToolContext, ToolError
from app.verification import web
from app.verification.backends import SingleStageBackend, WebsiteBackend, judge_website
from app.verification.results import Criterion, Verdict
from tests.unit.test_coding_tools import Rig, passed
from tests.unit.test_web_verification import EGRESS, IMAGE_ID, OK_STATIC, FakeDocker, react_report, report


@pytest.fixture
def approved(monkeypatch):
    monkeypatch.setattr(web, "_APPROVED", {IMAGE_ID})


def verifier(tmp_path, docker):
    return web.WebVerifier(EGRESS, run=docker, work_dir=tmp_path, pids=64)


@pytest.mark.parametrize("kind", ["static-html", "react-vite"])
def test_multi_stage_backend_reports_evidence_without_inventing_acceptance(tmp_path, approved, kind):
    docker = FakeDocker(verify_out=report(OK_STATIC) if kind == "static-html" else react_report())
    files = {"site/index.html": b"<h1>Site</h1>", "site/package.json": b"{}"}
    result = WebsiteBackend(verifier(tmp_path, docker), files, kind, "site").run()
    assert result.execution.status is ExecStatus.SUCCEEDED
    assert result.website.cleanup == "removed"
    assert result.website.pages == ({"path": "/"},)
    assert result.website.evidence["files"] == sorted(files)
    judged = judge_website(result.website, (Criterion("AC-001", "MUST"),), "digest")
    assert judged.verdict is Verdict.NOT_RUN and judged.counts.ran == 0
    assert judged.fingerprint == "digest"
    assert all(str(c.status) != "PASSED" for c in judged.criteria)
    args = docker.run_args("verify-web.mjs")
    assert args[args.index("--pids-limit") + 1] == "64"


@pytest.mark.parametrize("stage,kind", [("install-deps.mjs", "react-vite"), ("verify-web.mjs", "static-html")])
def test_nonzero_exit_overrules_a_passing_report(tmp_path, approved, stage, kind):
    class ExitFailure(FakeDocker):
        def __call__(self, argv, **kw):
            done = super().__call__(argv, **kw)
            return subprocess.CompletedProcess(argv, 7, done.stdout, "") if stage in " ".join(argv) else done
    docker = ExitFailure(verify_out=report(OK_STATIC))
    result = WebsiteBackend(verifier(tmp_path, docker), {"package.json": b"{}", "index.html": b"x"}, kind).run()
    assert result.website.verdict == "FAILED"
    assert result.website.cleanup == "removed"
    assert "7" in result.website.reason


@pytest.mark.parametrize("stage,kind", [("install-deps.mjs", "react-vite"), ("verify-web.mjs", "static-html")])
def test_timeouts_are_distinct_and_all_resources_are_checked(tmp_path, approved, stage, kind):
    class Timeout(FakeDocker):
        def __call__(self, argv, **kw):
            done = super().__call__(argv, **kw)
            if stage in " ".join(argv):
                raise subprocess.TimeoutExpired(argv, 1)
            return done
    docker = Timeout(verify_out="")
    result = WebsiteBackend(verifier(tmp_path, docker), {"package.json": b"{}"}, kind).run()
    assert result.execution.status is ExecStatus.TIMEOUT
    assert result.website.cleanup == "removed"
    assert sum("--filter" in c for c in docker.calls) == 5


def test_cancellation_during_install_stops_verify_but_not_cleanup(tmp_path, approved):
    cancel = threading.Event()
    class Cancel(FakeDocker):
        def __call__(self, argv, **kw):
            done = super().__call__(argv, **kw)
            if "install-deps.mjs" in " ".join(argv):
                cancel.set()
            return done
    docker = Cancel(verify_out="")
    result = WebsiteBackend(verifier(tmp_path, docker), {"package.json": b"{}"}, "react-vite").run(cancel)
    assert result.execution.status is ExecStatus.CANCELLED
    assert result.website.cleanup == "removed"
    assert not any("verify-web.mjs" in " ".join(c) for c in docker.calls)
    assert sum("--filter" in c for c in docker.calls) == 5


def test_pre_cancel_starts_no_process(tmp_path, approved):
    cancel = threading.Event()
    cancel.set()
    docker = FakeDocker(verify_out="")
    result = WebsiteBackend(verifier(tmp_path, docker), {}, "static-html").run(cancel)
    assert result.execution.status is ExecStatus.CANCELLED and docker.calls == []


def test_image_inspection_timeout_is_not_an_unhandled_exception(tmp_path):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)
    result = WebsiteBackend(verifier(tmp_path, timeout), {}, "static-html").run()
    assert result.execution.status is ExecStatus.TIMEOUT
    assert result.website.cleanup == "not_needed"


def test_network_setup_failure_retains_cleanup_evidence(tmp_path, approved):
    class NetworkFailure(FakeDocker):
        def __call__(self, argv, **kw):
            done = super().__call__(argv, **kw)
            if argv[1:3] == ["network", "create"]:
                return subprocess.CompletedProcess(argv, 1, "", "unavailable")
            return done
    docker = NetworkFailure(verify_out="")
    result = WebsiteBackend(verifier(tmp_path, docker), {"package.json": b"{}"}, "react-vite").run()
    assert result.website.verdict == "NOT_RUN" and result.website.evidence["phase"] == "install"
    assert result.website.cleanup == "removed"
    assert not any(c[0] == "run" for c in docker.calls)


def test_real_runner_is_bounded_and_receives_cancellation(tmp_path, approved, monkeypatch):
    from app.sandbox.process import ProcessResult
    calls = []
    cancel = threading.Event()
    def run(self, argv, **kwargs):
        calls.append((argv, kwargs))
        if "inspect" in argv:
            return ProcessResult(0, stdout=IMAGE_ID.encode())
        if "/opt/verify/verify-web.mjs" in argv:
            return ProcessResult(None, output_limit=True)
        return ProcessResult(0)
    monkeypatch.setattr(web.SubprocessRunner, "run", run)
    result = web.WebVerifier(EGRESS, work_dir=tmp_path, capture_bytes=2048).verify({"index.html": b"x"}, "static-html", cancel=cancel)
    assert result.exec_status == "OUTPUT_LIMIT" and result.cleanup == "removed"
    active = next(kw for argv, kw in calls if "/opt/verify/verify-web.mjs" in argv)
    assert active["capture_bytes"] == 2048 and active["cancel"] is cancel
    assert all(kw["cancel"] is None for argv, kw in calls if "--filter" in argv)


@pytest.mark.parametrize("failure", ["remaining", "unavailable", "timeout"])
def test_cleanup_uncertainty_cannot_pass_and_does_not_skip_other_resources(tmp_path, approved, failure):
    class CleanupFailure(FakeDocker):
        def __call__(self, argv, **kw):
            done = super().__call__(argv, **kw)
            if "--filter" in argv:
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(argv, 1)
                return subprocess.CompletedProcess(argv, 1 if failure == "unavailable" else 0, "still-there", "")
            return done
    docker = CleanupFailure(verify_out=report(OK_STATIC))
    result = WebsiteBackend(verifier(tmp_path, docker), {"index.html": b"x"}, "static-html").run()
    assert result.website.verdict == "NOT_RUN" and result.execution.cleanup == "failed"
    assert sum("--filter" in c for c in docker.calls) == 5


@pytest.mark.parametrize("mutation", ["string_ok", "skipped_browser", "string_ran", "missing_stage"])
def test_malformed_or_skipped_browser_evidence_is_not_a_pass(mutation):
    stages = [dict(s) for s in OK_STATIC]
    if mutation == "string_ok":
        stages[-1]["ok"] = "false"
    elif mutation == "string_ran":
        stages[-1]["ran"] = "true"
    elif mutation == "skipped_browser":
        stages[-1]["skipped"] = True
    else:
        stages.pop(0)
    assert not web.judge("static-html", report(stages)).passed


def test_python_backend_preserves_the_original_result(tmp_path):
    from app.sandbox.policy import build_spec
    rig = Rig(tmp_path)
    spec = build_spec("python-unittest", None, workspace=rig.h.workspace, project_id=rig.project_id, settings=rig.h.settings)
    expected = passed(spec)
    result = SingleStageBackend(FakeSandbox(lambda _: expected), spec).run()
    assert result.execution == expected and result.website is None


def _context(rig, session):
    run = session.get(Run, rig.run_id)
    project = session.get(Project, rig.project_id)
    project.stack, run.state = "static-html", RunState.IMPLEMENTING
    return ToolContext(session, run, project, rig.h.settings, rig.h.events, rig.h.catalog, workspace=rig.h.workspace)


def test_real_release_gate_still_refuses_website_submit(tmp_path, approved):
    rig = Rig(tmp_path)
    with rig.h.sessions.begin() as session:
        ctx = _context(rig, session)
        with pytest.raises(ToolError, match="blocked"):
            coding._begin_submit(rig.runtime, ctx, coding.SubmitImplementationArgs(summary="A site"))


@pytest.mark.parametrize("broken", [False, True])
def test_submit_persists_browser_evidence_and_never_delivers_smoke_only(tmp_path, approved, monkeypatch, broken):
    # Test-only readiness: production remains disabled. Real DB, candidate and state transitions.
    monkeypatch.setattr(coding, "readiness", lambda _: (True, "test-only"))
    rig = Rig(tmp_path)
    docker = FakeDocker(verify_out=report(OK_STATIC) if not broken else report([{"name": "assets", "ok": False, "detail": "missing logo"}]))
    runtime = replace(rig.runtime, web=verifier(tmp_path / "web", docker))
    rig.candidates.open_or_create(rig.run_id, rig.original)
    rig.fs.write_text(rig.run_id, "index.html", "<h1>A site</h1>")
    with rig.h.sessions.begin() as session:
        ctx = _context(rig, session)
        plan = coding._begin_submit(runtime, ctx, coding.SubmitImplementationArgs(summary="A site"))
        assert ctx.run.state is RunState.VERIFYING
    result = coding._run_verification(plan, None, lambda *args: None)
    with rig.h.sessions.begin() as session:
        ctx = _context(rig, session)
        ctx.run.state = RunState.VERIFYING
        coding._finish_submit(runtime, ctx, plan, result)
        assert ctx.run.state is not RunState.DOCUMENTING
        saved = json.loads(ctx.run.verification_json)
        assert saved["verdict"] != "PASSED"
        assert rig.untouched()
        events = coding.build_report(session, ctx.run, ctx.project, gpt="FakeLLM", sandbox="FakeSandbox")["website_verifications"]
        assert events[-1]["stages"] and events[-1]["cleanup"] == "removed"
        assert events[-1]["verdict"] == ("FAILED" if broken else "PASSED")
