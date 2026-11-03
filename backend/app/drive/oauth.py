"""Google OAuth for Drive, with the contracts of SECURITY.md §7. **Never run against Google; tested only against a
simulated provider (app/drive/fixtures.py: SimulatedOAuthProvider).**

- **Consent:** authorization code with PKCE (S256).
  - A random `state` is bound to the project and to a browser binding (an HttpOnly cookie set when the person starts
    consent); it expires after `STATE_TTL` seconds and is used once.
  - The PKCE verifier lives only in the pending record and goes only to the token exchange.
  - The redirect URI is validated strictly when configured, and only that exact URI is used.
- **Callback:** a missing, unknown, expired, reused or mismatched `state`, a wrong binding, a Google error, a malformed
  code, a scope beyond read-only, or a missing refresh token each refuse, and never grant.
- **Storage:** only the refresh token is kept, encrypted by a `TokenCipher`.
  - The approved cipher is Fernet (`cryptography`) with `DEVPILOT_TOKEN_KEY`. **`cryptography` is not installed here**,
    so `FernetCipher` cannot be built, and completing consent **fails closed** before any code is exchanged.
  - No other algorithm stands in for it.
  - Access tokens stay in memory until they expire.
- **Refresh:** one at a time per grant. An `invalid_grant` deletes the local credential and invalidates the grant; an
  unreachable provider changes nothing and is reported.
- **Revocation:** the local credential is deleted first, and then Google is asked to revoke it. The two outcomes are
  reported separately (`RevocationResult`): local deletion can succeed while remote revocation fails.

`GoogleOAuthProvider` speaks to oauth2.googleapis.com through the `google_drive` egress entry. It is tested only with an
in-process transport.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import SecretStr

from app.core.egress import EgressPolicy
from app.drive.access import Grant, GrantStatus, RevocationResult, check_scopes
from app.drive.model import READ_ONLY_SCOPES, DriveAccessRefused, DriveAuthorizationRequired, DriveUnavailable

CALLBACK_PATH = "/oauth/google/callback"
BINDING_COOKIE = "devpilot_oauth_binding"
STATE_TTL = 600.0
_CODE = re.compile(r"^[A-Za-z0-9/_.\-]{10,2048}$")
_REFRESH_SKEW = 60.0  # seconds before expiry when an access token is refreshed


class OAuthError(Exception):
    """The provider refused or failed. The message never holds a token, a code or a secret."""


class InvalidGrant(OAuthError):
    """The code or refresh token is no longer valid: consent must be given again."""


class ProviderUnavailable(OAuthError):
    """The provider did not answer usefully. Nothing was changed."""


class CredentialStorageUnavailable(DriveAuthorizationRequired):
    """No approved encryption is available, so no refresh token may be stored. Consent fails closed."""


@dataclass(frozen=True)
class TokenSet:
    access_token: str = field(repr=False)
    expires_in: int
    scope: str
    refresh_token: str | None = field(default=None, repr=False)


class OAuthProvider(Protocol):
    simulated: bool

    def exchange(self, code: str, verifier: str, redirect_uri: str) -> TokenSet: ...

    def refresh(self, refresh_token: str) -> TokenSet: ...

    def revoke(self, token: str) -> None:
        """Raises InvalidGrant when the token was already invalid, OAuthError when revocation was not confirmed."""
        ...


class TokenCipher(Protocol):
    name: str

    def encrypt(self, plaintext: bytes) -> bytes: ...

    def decrypt(self, ciphertext: bytes) -> bytes: ...


class FernetCipher:
    """SECURITY.md §7's cipher: Fernet (AES-128-CBC with HMAC-SHA256) from `cryptography`, keyed by DEVPILOT_TOKEN_KEY."""

    name = "fernet"

    def __init__(self, key: SecretStr | None) -> None:
        try:
            from cryptography.fernet import Fernet, InvalidToken
        except ImportError as exc:
            raise CredentialStorageUnavailable("refresh tokens cannot be stored: the approved encryption (cryptography's Fernet) is not installed") from exc
        if key is None:
            raise CredentialStorageUnavailable("refresh tokens cannot be stored: DEVPILOT_TOKEN_KEY is not configured")
        try:
            self._fernet = Fernet(key.get_secret_value())
        except ValueError as exc:
            raise CredentialStorageUnavailable("refresh tokens cannot be stored: DEVPILOT_TOKEN_KEY is not a valid Fernet key") from exc
        self._invalid = InvalidToken

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        try:
            return self._fernet.decrypt(ciphertext)
        except self._invalid as exc:
            raise CredentialStorageUnavailable("a stored credential cannot be decrypted with the current key") from exc


def build_cipher(key: SecretStr | None) -> tuple[TokenCipher | None, str | None]:
    """The approved cipher, or None and why. Never a weaker substitute."""
    try:
        return FernetCipher(key), None
    except CredentialStorageUnavailable as exc:
        return None, str(exc)


@dataclass(frozen=True)
class StoredCredential:
    project_id: str
    ciphertext: bytes = field(repr=False)
    scopes: frozenset[str]
    stored_at: float


class CredentialStore:
    """Encrypted refresh tokens by grant. It refuses anything but ciphertext bytes, and keeps them in memory only."""

    def __init__(self) -> None:
        self._items: dict[str, StoredCredential] = {}

    def put(self, grant_id: str, credential: StoredCredential) -> None:
        if not isinstance(credential.ciphertext, bytes) or not credential.ciphertext:
            raise CredentialStorageUnavailable("only an encrypted credential can be stored")
        self._items[grant_id] = credential

    def get(self, grant_id: str) -> StoredCredential | None:
        return self._items.get(grant_id)

    def delete(self, grant_id: str) -> bool:
        return self._items.pop(grant_id, None) is not None


def check_redirect_uri(uri: str) -> str:
    """The one redirect URI: https, or http to a loopback address with a port; the callback path exactly; no user, query
    or fragment. Anything else is a configuration error."""
    parts = urlsplit(uri)
    host = (parts.hostname or "").lower()
    loopback = host == "localhost" or _loopback(host)
    if parts.scheme not in ("https", "http") or (parts.scheme == "http" and not (loopback and parts.port)):
        raise ValueError("the OAuth redirect URI must be https, or http to a loopback address with a port")
    if parts.username or parts.password or parts.query or parts.fragment or "*" in uri:
        raise ValueError("the OAuth redirect URI may not carry a user, a query, a fragment or a wildcard")
    if parts.path != CALLBACK_PATH:
        raise ValueError(f"the OAuth redirect URI's path must be exactly {CALLBACK_PATH}")
    return uri


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _digest(binding: str) -> bytes:
    return hashlib.sha256(binding.encode("utf-8")).digest()


@dataclass(frozen=True)
class ConsentRequest:
    """What the person's browser gets: the consent URL, and the binding for its cookie. Never shown to GPT."""

    url: str
    state: str = field(repr=False)
    binding: str = field(repr=False)


@dataclass
class _Pending:
    grant_id: str
    project_id: str
    verifier: str = field(repr=False)
    redirect_uri: str
    binding_hash: bytes = field(repr=False)
    expires_at: float
    consent: ConsentRequest | None = field(default=None, repr=False)  # handed out once


class GoogleOAuthAuthorizer:
    name = "google-oauth"
    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

    def __init__(
        self, client_id: str | None, redirect_uri: str, *, provider: OAuthProvider | None = None, cipher: TokenCipher | None = None, cipher_problem: str | None = None,
        store: CredentialStore | None = None, clock: Callable[[], float] = time.time, state_ttl: float = STATE_TTL,
    ) -> None:  # fmt: skip
        self._client_id = client_id
        self._redirect = check_redirect_uri(redirect_uri)
        self._provider = provider
        self._cipher = cipher
        self._cipher_problem = cipher_problem or ("no approved cipher is configured" if cipher is None else None)
        self.store = store or CredentialStore()
        self._clock = clock
        self._ttl = state_ttl
        self._pending: dict[str, _Pending] = {}  # by state
        self._access: dict[str, tuple[str, float]] = {}  # grant -> (access token, expiry)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.simulated = bool(provider is not None and provider.simulated)
        self.on_invalidated: Callable[[str], None] | None = None  # set by the runtime: a grant Google no longer accepts

    # --- Consent ----------------------------------------------------------------------------------------------------

    def authorize(self, project_id: str, folders: tuple[str, ...], scopes: frozenset[str]) -> Grant:
        if not self._client_id:
            raise DriveAuthorizationRequired("GOOGLE_OAUTH_CLIENT_ID is not configured")
        wanted = check_scopes(scopes)
        verifier, state, binding = secrets.token_urlsafe(48), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        query = {"client_id": self._client_id, "redirect_uri": self._redirect, "response_type": "code", "scope": " ".join(sorted(wanted)), "state": state,
                 "code_challenge": _challenge(verifier), "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent", "include_granted_scopes": "false"}  # fmt: skip
        grant = Grant(f"grant-{secrets.token_hex(8)}", project_id, folders, wanted, GrantStatus.PENDING_CONSENT, self.simulated, "pending", _stamp())
        with self._guard:
            self._expire()
            self._pending[state] = _Pending(grant.id, project_id, verifier, self._redirect, _digest(binding), self._clock() + self._ttl, ConsentRequest(f"{self.AUTH_URL}?{urlencode(query)}", state, binding))
        return grant

    def consent(self, grant_id: str, project_id: str) -> ConsentRequest:
        """The consent URL and browser binding for a pending grant of this project. Handed out once."""
        with self._guard:
            self._expire()
            pending = next((p for p in self._pending.values() if p.grant_id == grant_id and p.project_id == project_id), None)
            if pending is None or pending.consent is None:
                raise DriveAuthorizationRequired("there is no consent waiting to start for that authorization in this project")
            request, pending.consent = pending.consent, None
        return request

    def complete(self, *, state: str | None, code: str | None, error: str | None, binding: str | None) -> tuple[str, str]:
        """The callback. Returns (grant ID, project ID) once consent is complete; refuses everything else, and a
        refused or replayed `state` can never be used again."""
        with self._guard:
            self._expire(keep=state)
            pending = self._pending.pop(state, None) if isinstance(state, str) else None  # removed now: used once, whatever follows
        if pending is None:
            raise DriveAccessRefused("the consent answer has an unknown, expired or already used state")
        if pending.expires_at <= self._clock():
            raise DriveAccessRefused("the consent answer came after its state expired")
        if not isinstance(binding, str) or not secrets.compare_digest(_digest(binding), pending.binding_hash):
            raise DriveAccessRefused("the consent answer did not come from the browser that started it")
        if error is not None:
            raise DriveAuthorizationRequired(f"Google consent was not given ({str(error)[:40]})")
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise DriveAccessRefused("the consent answer has no valid authorization code")
        if self._cipher is None:  # fail closed before any code is exchanged: nothing could be stored safely
            raise CredentialStorageUnavailable(self._cipher_problem or "no approved cipher is configured")
        if self._provider is None:
            raise DriveAuthorizationRequired("no OAuth provider is configured")
        try:
            tokens = self._provider.exchange(code, pending.verifier, pending.redirect_uri)
        except InvalidGrant as exc:
            raise DriveAuthorizationRequired("Google did not accept the authorization code") from exc
        except OAuthError as exc:
            raise DriveUnavailable(f"the authorization code could not be exchanged: {exc}") from exc
        problem = self._token_problem(tokens, need_refresh=True)
        if problem is not None:
            self._discard(tokens)
            raise DriveAccessRefused(problem)
        assert tokens.refresh_token is not None
        self.store.put(pending.grant_id, StoredCredential(pending.project_id, self._cipher.encrypt(tokens.refresh_token.encode()), frozenset(tokens.scope.split()), self._clock()))
        self._access[pending.grant_id] = (tokens.access_token, self._clock() + max(int(tokens.expires_in), 0))
        return pending.grant_id, pending.project_id

    # --- Use --------------------------------------------------------------------------------------------------------

    def token(self, grant_id: str) -> str:
        cached = self._access.get(grant_id)
        if cached is not None and cached[1] - _REFRESH_SKEW > self._clock():
            return cached[0]
        with self._guard:
            lock = self._locks.setdefault(grant_id, threading.Lock())
        with lock:  # one refresh at a time per grant
            cached = self._access.get(grant_id)
            if cached is not None and cached[1] - _REFRESH_SKEW > self._clock():
                return cached[0]
            stored = self.store.get(grant_id)
            if stored is None or self._cipher is None or self._provider is None:
                raise DriveAuthorizationRequired("there is no stored Google authorization: consent must be given")
            refresh = self._cipher.decrypt(stored.ciphertext).decode()
            try:
                tokens = self._provider.refresh(refresh)
            except InvalidGrant as exc:
                self._forget(grant_id)
                if self.on_invalidated is not None:
                    self.on_invalidated(grant_id)
                raise DriveAuthorizationRequired("Google no longer accepts this authorization: consent must be given again") from exc
            except OAuthError as exc:
                raise DriveUnavailable(f"the access token could not be refreshed: {exc}") from exc
            problem = self._token_problem(tokens, need_refresh=False)
            if problem is not None:
                self._forget(grant_id)
                raise DriveAccessRefused(problem)
            if tokens.refresh_token and tokens.refresh_token != refresh:  # Google rotated it
                self.store.put(grant_id, StoredCredential(stored.project_id, self._cipher.encrypt(tokens.refresh_token.encode()), stored.scopes, self._clock()))
            self._access[grant_id] = (tokens.access_token, self._clock() + max(int(tokens.expires_in), 0))
            return tokens.access_token

    def revoke(self, grant: Grant) -> RevocationResult:
        """Delete the local credential first, then ask Google to revoke it. Both outcomes are reported."""
        stored = self.store.get(grant.id)
        refresh = None
        if stored is not None and self._cipher is not None:
            try:
                refresh = self._cipher.decrypt(stored.ciphertext).decode()
            except CredentialStorageUnavailable:
                refresh = None
        access = self._access.get(grant.id, (None, 0.0))[0]
        self._forget(grant.id)
        with self._guard:
            for state in [s for s, p in self._pending.items() if p.grant_id == grant.id]:
                del self._pending[state]
        token = refresh or access
        if token is None or self._provider is None:
            return RevocationResult(True, "not_attempted", "no credential was held, so Google was not asked")
        try:
            self._provider.revoke(token)
        except InvalidGrant:
            return RevocationResult(True, "already_invalid", "Google reports the token was already invalid")
        except OAuthError as exc:
            return RevocationResult(True, "failed", f"Google did not confirm the revocation ({exc}); remove DevPilot's access at myaccount.google.com/permissions")
        return RevocationResult(True, "simulated" if self.simulated else "revoked", "the provider confirmed the revocation")

    # --- Internals --------------------------------------------------------------------------------------------------

    def _token_problem(self, tokens: TokenSet, *, need_refresh: bool) -> str | None:
        granted = frozenset(tokens.scope.split())
        if not granted or not granted <= READ_ONLY_SCOPES:
            return "Google granted a scope beyond read-only Drive access, so the authorization was refused"
        if need_refresh and not tokens.refresh_token:
            return "Google returned no refresh token; consent must be given again"
        if not tokens.access_token:
            return "Google returned no access token"
        return None

    def _discard(self, tokens: TokenSet) -> None:
        """Tokens that will not be kept are revoked at once (revoking the refresh token revokes its access tokens too)."""
        token = tokens.refresh_token or tokens.access_token
        if token and self._provider is not None:
            try:
                self._provider.revoke(token)
            except OAuthError:
                pass  # best effort: nothing was stored either way

    def _forget(self, grant_id: str) -> None:
        self.store.delete(grant_id)
        self._access.pop(grant_id, None)

    def _expire(self, keep: str | None = None) -> None:
        now = self._clock()
        for state in [s for s, p in self._pending.items() if p.expires_at <= now and s != keep]:
            del self._pending[state]


class GoogleOAuthProvider:
    """Google's token and revocation endpoints. **Never run against Google**; tested with an in-process transport."""

    simulated = False
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    REVOKE_URL = "https://oauth2.googleapis.com/revoke"

    def __init__(self, egress: EgressPolicy, client_id: str, client_secret: SecretStr | None, *, client: httpx.Client | None = None, timeout: float = 20.0) -> None:
        self._egress = egress
        self._client_id = client_id
        self._secret = client_secret
        self._client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "DevPilot-AI"})

    def exchange(self, code: str, verifier: str, redirect_uri: str) -> TokenSet:
        return _tokens(self._post(self.TOKEN_URL, {"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri, **self._client_form()}))

    def refresh(self, refresh_token: str) -> TokenSet:
        return _tokens(self._post(self.TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": refresh_token, **self._client_form()}))

    def revoke(self, token: str) -> None:
        self._post(self.REVOKE_URL, {"token": token})

    def _client_form(self) -> dict[str, str]:
        form = {"client_id": self._client_id}
        if self._secret is not None:
            form["client_secret"] = self._secret.get_secret_value()
        return form

    def _post(self, url: str, form: dict[str, str]) -> dict[str, Any]:
        decision = self._egress.check("google_drive", url)
        if not decision.allowed:
            raise OAuthError(f"egress policy refused {httpx.URL(url).host}: {decision.reason}")
        try:
            answer = self._client.post(url, data=form, follow_redirects=False)
        except httpx.TransportError as exc:
            raise ProviderUnavailable(f"Google could not be reached ({type(exc).__name__})") from exc
        if len(answer.content) > 100_000:
            raise OAuthError("Google's answer is over the size limit")
        try:
            body = answer.json() if answer.content else {}
        except ValueError:
            body = {}
        reason = str(body.get("error", "")) if isinstance(body, dict) else ""
        if answer.status_code == 200:
            return body if isinstance(body, dict) else {}
        if reason in ("invalid_grant", "invalid_token"):
            raise InvalidGrant(f"Google answered {answer.status_code} ({reason})")
        if answer.status_code == 429 or answer.status_code >= 500:
            raise ProviderUnavailable(f"Google answered {answer.status_code}")
        raise OAuthError(f"Google answered {answer.status_code} ({reason[:40] or 'no reason'})")


def _tokens(body: dict[str, Any]) -> TokenSet:
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise OAuthError("Google's token answer has no access token")
    try:
        expires_in = int(body.get("expires_in") or 0)
    except (TypeError, ValueError) as exc:
        raise OAuthError("Google's token answer has an invalid expiry") from exc
    refresh = body.get("refresh_token")
    return TokenSet(access, expires_in, str(body.get("scope", "")), refresh if isinstance(refresh, str) and refresh else None)


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
