"""Evaluation routes from the evaluation upgrade, integrated with this backend.

- `GET /api/v1/evaluation/tools` and `GET /api/v1/evaluation/tools/compare`: the GPT functions actually registered here, with
  their risk, states and approval needs. Registration is not proof of live connectivity; the Tools page (`/api/v1/tools`) holds
  the reviewed facts, readiness and evidence.
- `POST /api/v1/evaluation/source-gaps`: conservative word-match triage of a project's files against requirements; never proof.
- `POST /api/v1/evaluation/document-diff`: the exact text difference between two versions of a document. No interpretation.
- `POST /api/v1/evaluation/github/preview` then `POST /api/v1/evaluation/github/publish`: a delivered, verified run's files as a
  draft pull request. The preview lists every file and its SHA-256, and is valid once for ten minutes. Publishing needs the exact
  typed confirmation, re-reads and re-fingerprints the verified files, and writes through the person's GitHub connection (or
  GITHUB_TOKEN). It makes one new `devpilot/*` branch, one commit and a draft pull request: no merge, deploy or force-push.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field, StringConstraints

from app.api.deps import SessionDep, WorkspaceDep, load_run
from app.api.runs import final_report
from app.core.auth import require_api_token
from app.evaluation.gaps import compare_document_versions, compare_requirements
from app.evaluation.github_publish import PreviewStore, PublishRefused, build_plan, publish
from app.evaluation.tool_explain import compare_tools, tool_catalog
from app.repos.github_write import GitHubWriteError, GitHubWriter
from app.schemas.api import StrictInput
from app.verification import artifact

router = APIRouter(tags=["evaluation"], dependencies=[Depends(require_api_token)])
ProjectId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class GapItem(StrictInput):
    identifier: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=3, max_length=1200)


class GapRequest(StrictInput):
    project_id: ProjectId
    requirements: list[GapItem] = Field(min_length=1, max_length=30)


class VersionRequest(StrictInput):
    previous_text: str = Field(max_length=150_000)
    current_text: str = Field(max_length=150_000)
    previous_label: str = Field(min_length=1, max_length=100)
    current_label: str = Field(min_length=1, max_length=100)


class PreviewRequest(StrictInput):
    run_id: ProjectId
    repository: str = Field(min_length=3, max_length=150)
    base: str = Field(default="main", max_length=120)
    branch: str = Field(min_length=9, max_length=120)
    title: str = Field(min_length=4, max_length=160)
    body: str = Field(default="", max_length=10000)


class PublishRequest(StrictInput):
    preview_id: str = Field(min_length=20, max_length=100)
    confirmation: str = Field(min_length=1, max_length=250)


@router.get("/evaluation/tools")
def list_tools(request: Request) -> dict[str, Any]:
    return tool_catalog(request.app.state.registry)


@router.get("/evaluation/tools/compare")
def compare_registered_tools(request: Request, first: str, second: str) -> dict[str, Any]:
    try:
        return compare_tools(request.app.state.registry, first, second)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/evaluation/source-gaps")
def source_gaps(body: GapRequest, fs: WorkspaceDep) -> dict[str, Any]:
    if not fs.has_project(body.project_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such workspace; import a repository first")
    try:
        return compare_requirements([(i.identifier, i.description) for i in body.requirements], fs.list_files(body.project_id),
                                    lambda path: fs.read_bytes(body.project_id, path))  # fmt: skip
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/evaluation/document-diff")
def document_diff(body: VersionRequest) -> dict[str, Any]:
    return compare_document_versions(body.previous_text, body.current_text, body.previous_label, body.current_label)


def verified_snapshot(request: Request, session: Any, run_id: str) -> tuple[str, dict[str, bytes]]:
    """The files a verified delivery may ship, by the same rules as the ZIP (verification/artifact.decide)."""
    run = load_run(session, run_id)
    fs = request.app.state.workspace_fs
    report = final_report(request, session, run)
    try:
        paths = fs.list_files(run.project_id) if fs.has_project(run.project_id) else []
        decision = artifact.decide(report, paths, lambda path: fs.read_bytes(run.project_id, path), request.app.state.redactor)
    except Exception as exc:  # noqa: BLE001 - never disclose source text or credentials in an error
        raise HTTPException(status.HTTP_409_CONFLICT, "the verified project files could not be read") from exc
    if decision.reason is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This run is not eligible for a verified delivery: " + decision.reason)
    return artifact.verification_level(report) or "generated-tests", dict(decision.data)


@router.post("/evaluation/github/preview")
def preview_draft_pr(body: PreviewRequest, request: Request, session: SessionDep) -> dict[str, Any]:
    verification, files = verified_snapshot(request, session, body.run_id)
    try:
        plan = build_plan(body.run_id, body.repository, body.base, body.branch, body.title, body.body, files)
    except PublishRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    store: PreviewStore = request.app.state.evaluation_preview_store
    preview_id = store.save(plan)
    github = request.app.state.github.status()
    return {"preview_id": preview_id, "expires_in_seconds": 600, "verification_level": verification, "confirmation_required": f"publish {plan.repository}:{plan.branch}",
            "github_account": github.get("login"), "preview": plan.summary()}  # fmt: skip


def _token(request: Request) -> str:
    token = request.app.state.github.token()
    if token is None and request.app.state.settings.github_token is not None:
        token = request.app.state.settings.github_token.get_secret_value()
    if not token:
        raise PublishRefused("GitHub is not connected: connect it on the Tools page (or set GITHUB_TOKEN for the backend)")
    return token


@router.post("/evaluation/github/publish")
def publish_draft_pr(body: PublishRequest, request: Request) -> dict[str, Any]:
    """The person's explicit go-ahead only: no GPT tool can reach this route, and the typed confirmation must name the target."""
    store: PreviewStore = request.app.state.evaluation_preview_store
    try:
        plan = store.claim(body.preview_id)
        if body.confirmation != f"publish {plan.repository}:{plan.branch}":
            raise PublishRefused("the confirmation must match the exact repository and development branch")
        with request.app.state.session_factory() as session:
            project_id = load_run(session, plan.run_id).project_id  # before any write: a write is never left unrecorded
            _level, files = verified_snapshot(request, session, plan.run_id)
            current = build_plan(plan.run_id, plan.repository, plan.base, plan.branch, plan.title, plan.body, files)
            if current.fingerprint != plan.fingerprint:
                raise PublishRefused("the verified files changed after the preview; create a new preview")
        try:
            writer = GitHubWriter(request.app.state.egress, _token(request), client=getattr(request.app.state, "github_http", None))
        except GitHubWriteError as exc:
            raise PublishRefused(str(exc)) from exc
        result = publish(plan, writer)
        with request.app.state.session_factory.begin() as session:
            request.app.state.events.record(session, project_id=project_id, run_id=plan.run_id, type="github.published",
                                            payload={"mode": "draft_pull_request", "repository": result["repository"], "branch": result["branch"], "commit": result["commit"],
                                                     "pull_request_url": result["url"], "files": len(plan.files), "draft": True})  # fmt: skip
        return result
    except PublishRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
