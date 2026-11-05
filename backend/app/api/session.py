"""Signing the dashboard in and out (app/core/sessions.py).

- `GET /api/v1/session`: whether this browser is signed in. Needs nothing.
- `POST /api/v1/session` `{token, remember}`: signs in with the API token once; the answer sets the session cookie. The
  token is not kept by the browser.
- `POST /api/v1/session/exchange` `{code, remember}`: signs in with a one-time login link's code (the launcher prints it).
- `POST /api/v1/session/link` (Bearer token only): a new one-time login code, for a launcher or a terminal.
- `POST /api/v1/session/logout`: ends this browser's session.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import StringConstraints

from app.core.auth import token_matches
from app.core.sessions import COOKIE, CSRF_HEADER, MAX_AGE, SessionStore
from app.schemas.api import StrictInput

router = APIRouter(tags=["session"])
Secret = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class SignIn(StrictInput):
    token: Secret
    remember: bool = True


class Exchange(StrictInput):
    code: Secret
    remember: bool = True


def _store(request: Request) -> SessionStore:
    return request.app.state.web_sessions


def _set_cookie(request: Request, response: Response, *, remember: bool) -> None:
    raw = _store(request).create(remember=remember)
    secure = request.url.scheme == "https"
    response.set_cookie(COOKIE, raw, max_age=int(MAX_AGE.total_seconds()) if remember else None, path="/", httponly=True, samesite="strict", secure=secure)


@router.get("/session")
def session_status(request: Request) -> dict[str, Any]:
    signed_in = _store(request).check(request.cookies.get(COOKIE))
    return {"authenticated": signed_in, "via": "cookie" if signed_in else None, "csrf_header": CSRF_HEADER}


@router.post("/session")
def sign_in(body: SignIn, request: Request, response: Response) -> dict[str, Any]:
    store = _store(request)
    if store.locked_out():
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many wrong tokens: wait a minute and try again")
    if not token_matches(request, body.token.strip().strip("'\"")):
        store.failed()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "The backend did not accept this token", {"WWW-Authenticate": "Bearer"})
    _set_cookie(request, response, remember=body.remember)
    return {"authenticated": True, "via": "cookie"}


@router.post("/session/exchange")
def exchange(body: Exchange, request: Request, response: Response) -> dict[str, Any]:
    if not _store(request).redeem_link(body.code):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This sign-in link was already used or has expired: open the newest one the launcher printed",
                            {"WWW-Authenticate": "Bearer"})  # fmt: skip
    _set_cookie(request, response, remember=body.remember)
    return {"authenticated": True, "via": "cookie"}


@router.post("/session/link")
def new_link(request: Request) -> dict[str, str]:
    scheme, _, supplied = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not supplied or not token_matches(request, supplied):  # never by cookie: a session cannot mint sessions
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", {"WWW-Authenticate": "Bearer"})
    return {"code": _store(request).issue_link()}


@router.post("/session/logout")
def sign_out(request: Request, response: Response) -> dict[str, bool]:
    _store(request).end(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"authenticated": False}


def login_url(store: SessionStore, dashboard: str) -> str:
    """What a launcher prints: opening it signs the browser in once, with no token to paste."""
    return f"{dashboard.rstrip('/')}/#login={store.issue_link()}"
