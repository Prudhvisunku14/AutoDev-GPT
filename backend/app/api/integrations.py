"""Google Drive authorization over HTTP (SECURITY.md §7). **Never run against Google; tested with a simulated provider.**

- `POST /api/v1/integrations/google/consent` (API token): the consent URL for a pending grant of a project. It sets the
  browser binding as an HttpOnly cookie scoped to the callback path, so only the browser that started consent can
  finish it.
- `GET /oauth/google/callback` (no API token: Google sends the browser here): completes consent through
  `GoogleOAuthAuthorizer.complete`. A refused or replayed answer never grants, and its `state` is gone either way.
- `DELETE /api/v1/integrations/google/credentials?project_id=` (API token): ends every Google authorization of one
  project. It deletes DevPilot's credentials and everything read with them, then asks Google to revoke. Local deletion
  and remote revocation are reported separately, because the second can fail while the first has succeeded.

Every route answers 404 while Google Drive is not enabled (DRIVE_SOURCE=off, the default).
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import StringConstraints

from app.core.auth import require_api_token
from app.db.models import Project
from app.drive.access import GrantStatus
from app.drive.model import DriveAccessRefused, DriveAuthorizationRequired, DriveUnavailable
from app.drive.oauth import BINDING_COOKIE, CALLBACK_PATH, STATE_TTL, CredentialStorageUnavailable, GoogleOAuthAuthorizer
from app.schemas.api import StrictInput

router = APIRouter(tags=["integrations"])
GrantId = Annotated[str, StringConstraints(pattern=r"^grant-[0-9a-f]{16}$")]
ProjectId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class ConsentIn(StrictInput):
    project_id: ProjectId
    grant_id: GrantId


def _drive(request: Request) -> Any:
    runtime = getattr(request.app.state, "drive", None)
    if runtime is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Google Drive is not enabled")
    return runtime


def _oauth(request: Request) -> tuple[Any, GoogleOAuthAuthorizer]:
    runtime = _drive(request)
    if not isinstance(runtime.authorizer, GoogleOAuthAuthorizer):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Google consent is not configured")
    return runtime, runtime.authorizer


@router.post("/api/v1/integrations/google/consent", dependencies=[Depends(require_api_token)])
def start_consent(body: ConsentIn, request: Request, response: Response) -> dict[str, str]:
    _runtime, oauth = _oauth(request)
    try:
        consent = oauth.consent(body.grant_id, body.project_id)
    except DriveAuthorizationRequired as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    secure = urlsplit(request.app.state.settings.google_oauth_redirect_uri).scheme == "https"
    response.set_cookie(BINDING_COOKIE, consent.binding, max_age=int(STATE_TTL), path=CALLBACK_PATH, httponly=True, samesite="lax", secure=secure)
    return {"url": consent.url, "note": "Open this URL in the same browser. The answer is accepted once, within ten minutes."}


@router.get(CALLBACK_PATH, response_class=PlainTextResponse)
def consent_callback(request: Request, state: str | None = None, code: str | None = None, error: str | None = None) -> PlainTextResponse:
    runtime, oauth = _oauth(request)
    binding = request.cookies.get(BINDING_COOKIE)
    try:
        grant_id, project_id = oauth.complete(state=state, code=code, error=error, binding=binding)
        runtime.grants.activate(grant_id, project_id)
    except CredentialStorageUnavailable as exc:  # fail closed: nothing was exchanged or stored
        answer = PlainTextResponse(f"Google Drive access was not completed: {exc}", status.HTTP_503_SERVICE_UNAVAILABLE)
    except DriveUnavailable as exc:
        answer = PlainTextResponse(f"Google Drive access was not completed: {exc}", status.HTTP_502_BAD_GATEWAY)
    except (DriveAccessRefused, DriveAuthorizationRequired) as exc:
        answer = PlainTextResponse(f"Google Drive access was refused: {exc}", status.HTTP_400_BAD_REQUEST)
    else:
        with request.app.state.session_factory.begin() as session:
            request.app.state.events.record(session, project_id=project_id, type="drive.consent_completed", payload={"grant_id": grant_id})
        answer = PlainTextResponse("Google Drive read-only access was granted to DevPilot. You can close this window.")
    answer.delete_cookie(BINDING_COOKIE, path=CALLBACK_PATH)
    return answer


@router.delete("/api/v1/integrations/google/credentials", dependencies=[Depends(require_api_token)])
def delete_credentials(request: Request, project_id: Annotated[str, Query(pattern=r"^[0-9a-f]{32}$")]) -> dict[str, Any]:
    runtime = _drive(request)
    with request.app.state.session_factory.begin() as session:
        if session.get(Project, project_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
        results = []
        for grant in runtime.grants.for_project(project_id):
            if grant.status is GrantStatus.REVOKED:
                continue
            _grant, revocation, (documents, chunks) = runtime.revoke(grant.id, project_id)
            results.append({"grant_id": grant.id, "removed_documents": documents, "removed_chunks": chunks, **revocation.to_dict()})
        summary = {"project_id": project_id, "grants": results, "local_deleted": all(r["local_deleted"] for r in results),
                   "remote_revoked": all(r["remote_revocation"] in ("revoked", "already_invalid", "simulated", "not_attempted") for r in results)}  # fmt: skip
        request.app.state.events.record(session, project_id=project_id, type="drive.credentials_deleted", payload=summary)
    if not results:
        summary["note"] = "no Google authorization was held for this project"
    elif not summary["remote_revoked"]:
        summary["note"] = "DevPilot deleted its copy, but Google did not confirm the revocation: remove DevPilot's access at myaccount.google.com/permissions"
    return summary
