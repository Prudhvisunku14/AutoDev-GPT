"""Phase 7: dashboard sessions. The browser signs in once (token or one-time link) and holds only an HttpOnly cookie."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.core import sessions
from app.core.sessions import COOKIE, CSRF_HEADER
from app.db.models import WebSession
from app.main import create_app
from tests.helpers import API_TOKEN, make_settings

WRITE = {CSRF_HEADER: "1"}


def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(make_settings(tmp_path, log_level="ERROR")))


def test_signing_in_with_the_token_sets_an_httponly_strict_cookie_and_keeps_no_token(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        assert c.get("/api/v1/session").json()["authenticated"] is False
        answer = c.post("/api/v1/session", json={"token": f'  "{API_TOKEN}"  ', "remember": True})
        assert answer.status_code == 200 and API_TOKEN not in answer.text
        cookie = answer.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=" in cookie and API_TOKEN not in cookie
        assert c.get("/api/v1/projects").status_code == 200  # the cookie alone authenticates
        with c.app.state.session_factory() as s:
            [row] = s.query(WebSession).all()
        assert row.id_hash != c.cookies[COOKIE] and len(row.id_hash) == 64  # only its hash is stored


def test_a_cookie_write_without_the_request_header_is_refused(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        c.post("/api/v1/session", json={"token": API_TOKEN})
        refused = c.post("/api/v1/projects", json={"name": "P", "requirement_text": "Create a Python calculator with unit tests"})
        assert refused.status_code == 401 and CSRF_HEADER in refused.json()["detail"]
        assert c.post("/api/v1/projects", headers=WRITE, json={"name": "P", "requirement_text": "Create a Python calculator with unit tests"}).status_code == 201


def test_a_one_time_link_signs_in_once_and_only_a_bearer_can_mint_one(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        assert c.post("/api/v1/session/link").status_code == 401
        code = c.post("/api/v1/session/link", headers={"Authorization": f"Bearer {API_TOKEN}"}).json()["code"]
        assert c.post("/api/v1/session/exchange", json={"code": code}).status_code == 200
        assert c.get("/api/v1/session").json()["authenticated"] is True
        assert c.post("/api/v1/session/link", headers=WRITE).status_code == 401  # a session cannot mint more sessions
        c.cookies.clear()
        again = c.post("/api/v1/session/exchange", json={"code": code})
        assert again.status_code == 401 and "already used or has expired" in again.json()["detail"]


def test_wrong_tokens_are_refused_then_locked_out(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        for _ in range(sessions.MAX_FAILURES):
            assert c.post("/api/v1/session", json={"token": "wrong"}).status_code == 401
        assert c.post("/api/v1/session", json={"token": API_TOKEN}).status_code == 429


def test_signing_out_ends_the_session_and_an_idle_one_expires(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        c.post("/api/v1/session", json={"token": API_TOKEN})
        raw = c.cookies[COOKIE]
        c.post("/api/v1/session/logout")
        c.cookies.set(COOKIE, raw)
        assert c.get("/api/v1/projects").status_code == 401
        c.post("/api/v1/session", json={"token": API_TOKEN})
        with c.app.state.session_factory.begin() as s:
            for row in s.query(WebSession).all():
                row.last_used_at = row.last_used_at - sessions.IDLE - timedelta(seconds=1)
        assert c.get("/api/v1/projects").status_code == 401


def test_a_forged_or_absent_cookie_never_authenticates_and_bearer_still_works(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        c.cookies.set(COOKIE, "forged-value")
        assert c.get("/api/v1/projects").status_code == 401
        c.cookies.clear()
        assert c.get("/api/v1/projects", headers={"Authorization": f"Bearer {API_TOKEN}"}).status_code == 200


def test_the_login_link_points_at_the_dashboard(tmp_path: Path) -> None:
    from app.api.session import login_url

    app = create_app(make_settings(tmp_path, log_level="ERROR"))
    url = login_url(app.state.web_sessions, "http://localhost:5173/")
    assert url.startswith("http://localhost:5173/#login=") and app.state.web_sessions.redeem_link(url.split("=", 1)[1])
