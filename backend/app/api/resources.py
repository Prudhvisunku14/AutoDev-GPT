"""Resources a request names: discovery, access checks and the permission they need.

- `POST /api/v1/resources/discover`: the GitHub repositories and Drive files and folders in a text. Parsing only.
- `POST /api/v1/projects/{id}/resources/check`: reads each resource the project's requirement names, as far as DevPilot
  may: public repositories automatically, private ones through the person's GitHub connection, Drive files under this
  project's grant. The result is recorded (`resources.checked`) and given to GPT with the requirement.
- `GET /api/v1/projects/{id}/resources`: the last check.
- `POST /api/v1/projects/{id}/resources/google/authorize`: a pending Google grant for the Drive items the requirement names,
  and its consent URL (the browser binding is set as a cookie, as in integrations.py).
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import StringConstraints

from app.core.auth import require_api_token
from app.db.models import Project
from app.drive.model import READ_ONLY_SCOPES, DriveAuthorizationRequired, DriveError
from app.drive.oauth import BINDING_COOKIE, CALLBACK_PATH, STATE_TTL, GoogleOAuthAuthorizer
from app.repos.github_access import GitHubRepoClient
from app.schemas.api import StrictInput
from app.sources.check import check
from app.sources.discover import DriveResource, discover
from app.sources.events import CHECKED, latest_check

router = APIRouter(tags=["resources"], dependencies=[Depends(require_api_token)])
ProjectId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class DiscoverIn(StrictInput):
    text: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]


@router.post("/resources/discover")
def discover_resources(body: DiscoverIn) -> dict[str, Any]:
    return discover(body.text).to_dict()


def github_client(request: Request) -> GitHubRepoClient | None:
    """The reading client, with the person's connection when there is one; None when reading GitHub is off."""
    state = request.app.state
    if state.settings.repository_source != "github":
        return None
    token = state.github.token()
    if token is None and state.settings.github_token is not None:
        token = state.settings.github_token.get_secret_value()
    return GitHubRepoClient(state.egress, token=token, client=getattr(state, "github_http", None))


def _project(request: Request, project_id: str) -> Project:
    with request.app.state.session_factory() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
        session.expunge(project)
        return project


@router.post("/projects/{project_id}/resources/check")
def check_resources(project_id: ProjectId, request: Request) -> dict[str, Any]:
    project = _project(request, project_id)
    state = request.app.state
    found = discover(project.requirement_text)
    client = github_client(request)
    results = check(found, github=client, connected=state.github.token() is not None, connect_blocker=state.github.blocker(), drive=state.drive, project_id=project_id)
    payload = {"resources": results, "problems": list(found.problems), "needs_permission": [r["key"] for r in results if r.get("action")]}
    with state.session_factory.begin() as session:
        state.events.record(session, project_id=project_id, type=CHECKED, payload=payload)
    return payload


@router.get("/projects/{project_id}/resources")
def get_resources(project_id: ProjectId, request: Request) -> dict[str, Any]:
    project = _project(request, project_id)
    with request.app.state.session_factory() as session:
        last = latest_check(session, project_id)
    if last is not None:
        return last | {"checked": True}
    return discover(project.requirement_text).to_dict() | {"checked": False}


@router.post("/projects/{project_id}/resources/google/authorize")
def authorize_google(project_id: ProjectId, request: Request, response: Response) -> dict[str, str]:
    project = _project(request, project_id)
    runtime = request.app.state.drive
    if runtime is None or not isinstance(runtime.authorizer, GoogleOAuthAuthorizer):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Google Drive consent is not configured in this backend")
    items = tuple(r.drive_id for r in discover(project.requirement_text).resources if isinstance(r, DriveResource))
    if not items:
        raise HTTPException(status.HTTP_409_CONFLICT, "the requirement names no Google Drive file or folder")
    try:
        grant = runtime.authorizer.authorize(project_id, items, READ_ONLY_SCOPES)
        runtime.grants.add(grant)
        consent = runtime.authorizer.consent(grant.id, project_id)
    except (DriveAuthorizationRequired, DriveError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    with request.app.state.session_factory.begin() as session:
        request.app.state.events.record(session, project_id=project_id, type="drive.consent_requested", payload={"grant_id": grant.id, "items": list(items)})
    secure = urlsplit(request.app.state.settings.google_oauth_redirect_uri).scheme == "https"
    response.set_cookie(BINDING_COOKIE, consent.binding, max_age=int(STATE_TTL), path=CALLBACK_PATH, httponly=True, samesite="lax", secure=secure)
    return {"url": consent.url, "grant_id": grant.id}
