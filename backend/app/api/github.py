"""The person's GitHub connection over HTTP (app/repos/github_oauth.py). Nobody pastes a GitHub token into the dashboard.

- `GET /api/v1/integrations/github` (API token): configured or not and why, connected or not, the account and scopes.
- `POST /api/v1/integrations/github/connect` (API token): the GitHub consent URL, and the browser binding as an HttpOnly
  cookie scoped to the callback path, so only the browser that started can finish.
- `GET /oauth/github/callback` (no API token: GitHub sends the browser here): completes the connection once.
- `POST /api/v1/integrations/github/disconnect` (API token): deletes the stored token, then asks GitHub to revoke it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse

from app.core.auth import require_api_token
from app.repos.github_oauth import BINDING_COOKIE, CALLBACK_PATH, STATE_TTL, GitHubConnection, GitHubConnectionError, GitHubConnectionRefused

router = APIRouter(tags=["integrations"])

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>DevPilot AI: GitHub</title>
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;color:#1e1b4b}}</style></head>
<body><h1>{title}</h1><p>{message}</p><p>You can close this window and return to DevPilot.</p>
<script>if(window.opener){{setTimeout(function(){{window.close()}},1500)}}</script></body></html>"""


def _connection(request: Request) -> GitHubConnection:
    return request.app.state.github


@router.get("/api/v1/integrations/github", dependencies=[Depends(require_api_token)])
def github_status(request: Request) -> dict[str, Any]:
    return _connection(request).status()


@router.post("/api/v1/integrations/github/connect", dependencies=[Depends(require_api_token)])
def github_connect(request: Request, response: Response) -> dict[str, str]:
    try:
        start = _connection(request).start()
    except GitHubConnectionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    secure = urlsplit(request.app.state.settings.github_oauth_redirect_uri).scheme == "https"
    response.set_cookie(BINDING_COOKIE, start.binding, max_age=int(STATE_TTL), path=CALLBACK_PATH, httponly=True, samesite="lax", secure=secure)
    return {"url": start.url, "note": "Open this URL in the same browser. The answer is accepted once, within ten minutes."}


@router.get(CALLBACK_PATH, response_class=HTMLResponse)
def github_callback(request: Request, state: str | None = None, code: str | None = None, error: str | None = None) -> HTMLResponse:
    binding = request.cookies.get(BINDING_COOKIE)
    try:
        login = _connection(request).complete(state=state, code=code, error=error, binding=binding)
    except GitHubConnectionRefused as exc:
        page = HTMLResponse(_PAGE.format(title="GitHub was not connected", message=_html(str(exc))), status.HTTP_400_BAD_REQUEST)
    except GitHubConnectionError as exc:
        page = HTMLResponse(_PAGE.format(title="GitHub was not connected", message=_html(str(exc))), status.HTTP_502_BAD_GATEWAY)
    else:
        page = HTMLResponse(_PAGE.format(title="GitHub connected", message=f"DevPilot can now act for <strong>{_html(login)}</strong>, and every write still waits for your approval."))
    page.delete_cookie(BINDING_COOKIE, path=CALLBACK_PATH)
    return page


@router.post("/api/v1/integrations/github/disconnect", dependencies=[Depends(require_api_token)])
def github_disconnect(request: Request) -> dict[str, Any]:
    return _connection(request).disconnect()


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
