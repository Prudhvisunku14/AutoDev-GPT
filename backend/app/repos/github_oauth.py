"""The person's GitHub connection, managed by the backend: nobody pastes a token into the dashboard (SECURITY.md §10).

GitHub's OAuth web flow for an OAuth App the person registers (GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET):
- **Start:** a random `state`, a PKCE S256 challenge and a browser binding (an HttpOnly cookie scoped to the callback path).
  The state expires after `STATE_TTL` seconds and is used once.
- **Callback:** an unknown, expired, reused or mismatched state, a wrong binding, a GitHub error, a malformed code, or a
  granted scope narrower than asked each refuse, and never store anything. The code is exchanged only at
  github.com/login/oauth/access_token, through the `github` egress entry.
- **Storage:** the access token is encrypted with the approved cipher (Fernet, DEVPILOT_TOKEN_KEY) in the credentials
  directory. Without the cipher nothing is exchanged: consent fails closed. The token is decrypted only in memory, sent only
  to api.github.com in an Authorization header, and never logged, returned or put in a URL.
- **Disconnect:** the local credential is deleted first, then GitHub is asked to revoke the grant. The two outcomes are
  reported separately, because the second can fail while the first has succeeded.

**Tested only with an in-process transport; never run against GitHub here.**
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import SecretStr

from app.core.egress import EgressPolicy
from app.core.secret_store import EncryptedFileStore, SecretStoreUnavailable

CALLBACK_PATH = "/oauth/github/callback"
BINDING_COOKIE = "devpilot_github_binding"
STATE_TTL = 600.0
AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API = "https://api.github.com"
CREDENTIAL = "github"
_CODE = re.compile(r"^[A-Za-z0-9_-]{8,256}$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
# What each requested scope implies GitHub may grant instead: asking for repo and getting public_repo is narrower, so refused.
DEFAULT_SCOPES = ("repo",)


class GitHubConnectionError(Exception):
    """Consent or the exchange failed. The message never holds a token, a code or a secret."""


class GitHubConnectionRefused(GitHubConnectionError):
    """The answer was not acceptable (state, binding, code or scope): nothing was stored."""


@dataclass
class _Pending:
    verifier: str
    binding_hash: str
    expires_at: float


@dataclass(frozen=True)
class ConnectStart:
    url: str
    binding: str


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def check_redirect_uri(uri: str) -> str:
    parts = urlsplit(uri)
    loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme not in ("http", "https") or (parts.scheme == "http" and not loopback) or parts.path != CALLBACK_PATH or parts.query or parts.fragment:
        raise ValueError(f"GITHUB_OAUTH_REDIRECT_URI must be https (or http on loopback) and end in {CALLBACK_PATH}")
    return uri


class GitHubConnection:
    def __init__(
        self, egress: EgressPolicy, store: EncryptedFileStore, *, client_id: str | None, client_secret: SecretStr | None, redirect_uri: str,
        scopes: tuple[str, ...] = DEFAULT_SCOPES, client: httpx.Client | None = None, clock: Callable[[], float] = time.time, state_ttl: float = STATE_TTL,
    ) -> None:  # fmt: skip
        self._egress = egress
        self._store = store
        self._client_id = client_id
        self._secret = client_secret
        self._redirect = check_redirect_uri(redirect_uri)
        self._scopes = tuple(sorted(set(scopes)))
        self._client = client or httpx.Client(timeout=20.0, headers={"User-Agent": "DevPilot-AI"}, follow_redirects=False)
        self._clock = clock
        self._ttl = state_ttl
        self._pending: dict[str, _Pending] = {}
        self._guard = threading.Lock()
        self._cached: dict[str, Any] | None = None

    # --- Status -----------------------------------------------------------------------------------------------------

    def blocker(self) -> str | None:
        """What stops a connection from being made here, from configuration only."""
        if not self._client_id:
            return "GITHUB_OAUTH_CLIENT_ID is not configured: register a GitHub OAuth App and set it in the backend's .env"
        if self._secret is None:
            return "GITHUB_OAUTH_CLIENT_SECRET is not configured"
        if not self._store.available:
            return f"the GitHub token could not be stored encrypted: {self._store.problem}"
        return None

    def _credential(self) -> dict[str, Any] | None:
        if self._cached is None:
            try:
                self._cached = self._store.get(CREDENTIAL) if self._store.available else None
            except SecretStoreUnavailable:
                return None
        return self._cached

    def token(self) -> str | None:
        """The access token, decrypted in memory, or None when not connected."""
        found = self._credential()
        token = found.get("token") if found else None
        return token if isinstance(token, str) and token else None

    def status(self) -> dict[str, Any]:
        found = self._credential()
        return {
            "configured": self.blocker() is None,
            "blocker": self.blocker(),
            "connected": bool(found and found.get("token")),
            "login": found.get("login") if found else None,
            "scopes": found.get("scopes", []) if found else [],
            "connected_at": found.get("connected_at") if found else None,
            "requested_scopes": list(self._scopes),
        }

    # --- Consent ----------------------------------------------------------------------------------------------------

    def start(self) -> ConnectStart:
        problem = self.blocker()
        if problem is not None:
            raise GitHubConnectionError(problem)
        verifier, state, binding = secrets.token_urlsafe(48), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        query = {"client_id": self._client_id, "redirect_uri": self._redirect, "scope": " ".join(self._scopes), "state": state,
                 "code_challenge": _challenge(verifier), "code_challenge_method": "S256", "allow_signup": "false"}  # fmt: skip
        with self._guard:
            now = self._clock()
            self._pending = {k: v for k, v in self._pending.items() if v.expires_at > now}
            self._pending[state] = _Pending(verifier, _digest(binding), now + self._ttl)
        return ConnectStart(f"{AUTHORIZE_URL}?{urlencode(query)}", binding)

    def complete(self, *, state: str | None, code: str | None, error: str | None, binding: str | None) -> str:
        """The callback. Returns the connected login; refuses everything else. A state is gone after one use, whatever follows."""
        with self._guard:
            pending = self._pending.pop(state, None) if isinstance(state, str) else None
        if pending is None:
            raise GitHubConnectionRefused("the answer has an unknown, expired or already used state")
        if pending.expires_at <= self._clock():
            raise GitHubConnectionRefused("the answer came after its state expired")
        if not isinstance(binding, str) or not secrets.compare_digest(_digest(binding), pending.binding_hash):
            raise GitHubConnectionRefused("the answer did not come from the browser that started the connection")
        if error is not None:
            raise GitHubConnectionRefused(f"GitHub access was not given ({str(error)[:40]})")
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise GitHubConnectionRefused("the answer has no valid authorization code")
        problem = self.blocker()
        if problem is not None:  # fail closed before any exchange: nothing could be stored safely
            raise GitHubConnectionError(problem)
        assert self._secret is not None
        answer = self._post_json(TOKEN_URL, {"client_id": self._client_id, "client_secret": self._secret.get_secret_value(), "code": code,
                                             "redirect_uri": self._redirect, "code_verifier": pending.verifier})  # fmt: skip
        if "error" in answer or not isinstance(answer.get("access_token"), str):
            raise GitHubConnectionRefused(f"GitHub refused the code ({str(answer.get('error', 'no token'))[:40]})")
        token = answer["access_token"]
        granted = tuple(sorted(s.strip() for s in str(answer.get("scope", "")).split(",") if s.strip()))
        missing = [scope for scope in self._scopes if scope not in granted]
        if missing:
            raise GitHubConnectionRefused(f"GitHub granted less access than asked (missing {', '.join(missing)}); nothing was stored")
        login = self._login(token)
        self._store.put(CREDENTIAL, {"token": token, "login": login, "scopes": list(granted), "connected_at": int(self._clock())})
        self._cached = None
        return login

    def _post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self._egress.check("github", url).allowed:
            raise GitHubConnectionError("the egress allowlist does not allow github.com for the github component")
        try:
            response = self._client.post(url, json=body, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise GitHubConnectionError(f"GitHub could not be reached: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise GitHubConnectionError(f"GitHub answered HTTP {response.status_code} to the code exchange")
        try:
            data = response.json()
        except ValueError:
            raise GitHubConnectionError("GitHub's answer to the code exchange was not JSON") from None
        return data if isinstance(data, dict) else {}

    def _login(self, token: str) -> str:
        url = f"{API}/user"
        if not self._egress.check("github", url).allowed:
            raise GitHubConnectionError("the egress allowlist does not allow api.github.com for the github component")
        try:
            response = self._client.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        except httpx.HTTPError as exc:
            raise GitHubConnectionError(f"GitHub could not be reached: {type(exc).__name__}") from None
        login = response.json().get("login") if response.status_code == 200 else None
        if not isinstance(login, str) or not _LOGIN.fullmatch(login):
            raise GitHubConnectionRefused(f"GitHub did not confirm the account (HTTP {response.status_code}); nothing was stored")
        return login

    # --- Disconnect -------------------------------------------------------------------------------------------------

    def disconnect(self) -> dict[str, Any]:
        token = self.token()
        local = self._store.delete(CREDENTIAL)
        self._cached = None
        if token is None:
            return {"local_deleted": local, "remote_revocation": "not_attempted", "note": "no GitHub connection was stored"}
        url = f"{API}/applications/{self._client_id}/grant"
        if self._secret is None or not self._egress.check("github", url).allowed:
            return {"local_deleted": local, "remote_revocation": "not_attempted", "note": "revoke DevPilot at github.com/settings/applications"}
        try:
            response = self._client.request("DELETE", url, json={"access_token": token}, auth=(str(self._client_id), self._secret.get_secret_value()),
                                            headers={"Accept": "application/vnd.github+json"})  # fmt: skip
        except httpx.HTTPError:
            return {"local_deleted": local, "remote_revocation": "failed", "note": "GitHub could not be reached: revoke DevPilot at github.com/settings/applications"}
        if response.status_code == 204:
            return {"local_deleted": local, "remote_revocation": "revoked"}
        if response.status_code == 404:
            return {"local_deleted": local, "remote_revocation": "already_invalid"}
        return {"local_deleted": local, "remote_revocation": "failed", "note": f"GitHub answered HTTP {response.status_code}: revoke DevPilot at github.com/settings/applications"}
