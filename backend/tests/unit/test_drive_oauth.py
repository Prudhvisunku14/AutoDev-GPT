"""Google OAuth's security contracts (SECURITY.md §7), offline. **The provider is simulated** (fixtures.SimulatedOAuthProvider:
invented codes and tokens, never Google), and `TestOnlyCipher` below is a test double of the cipher interface. The
product has no cipher but Fernet, and `cryptography` is not installed, so the product fails closed (tested here too)."""

from __future__ import annotations

import base64
import threading
import warnings
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from app.config import REPO_ROOT
from app.core.egress import EgressPolicy
from app.drive import fixtures
from app.drive.access import GrantStatus
from app.drive.model import DRIVE_READONLY, READ_ONLY_SCOPES, DriveAccessRefused, DriveAuthorizationRequired, DriveUnavailable
from app.drive.oauth import (
    BINDING_COOKIE,
    CALLBACK_PATH,
    CredentialStorageUnavailable,
    GoogleOAuthAuthorizer,
    GoogleOAuthProvider,
    InvalidGrant,
    OAuthError,
    ProviderUnavailable,
    TokenSet,
    build_cipher,
    check_redirect_uri,
)
from app.drive.source import GoogleDriveSource
from app.main import create_app
from app.orchestrator.fake_llm import FakeLLM
from app.tools.adapters.drive import DriveRuntime
from tests.helpers import API_TOKEN, make_settings

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message=".*httpx.*")
    from fastapi.testclient import TestClient

REDIRECT = "http://127.0.0.1:8000/oauth/google/callback"
EGRESS = EgressPolicy.from_file(REPO_ROOT / "config" / "egress_allowlist.yaml")


class TestOnlyCipher:
    """A TEST DOUBLE of the TokenCipher interface. Not encryption; it lives only in this file."""

    __test__ = False
    name = "test-double"

    def encrypt(self, plaintext: bytes) -> bytes:
        return b"sealed:" + base64.b64encode(plaintext[::-1])

    def decrypt(self, ciphertext: bytes) -> bytes:
        assert ciphertext.startswith(b"sealed:")
        return base64.b64decode(ciphertext[7:])[::-1]


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def flow(*, cipher: object | None = TestOnlyCipher(), problem: str | None = None, **provider_options: object):  # noqa: ANN201
    provider, clock = fixtures.SimulatedOAuthProvider(**provider_options), Clock()  # type: ignore[arg-type]
    return GoogleOAuthAuthorizer("client-123.apps.googleusercontent.com", REDIRECT, provider=provider, cipher=cipher, cipher_problem=problem, clock=clock), provider, clock  # type: ignore[arg-type]


def begin(oauth: GoogleOAuthAuthorizer, provider: fixtures.SimulatedOAuthProvider, project: str = "p1"):  # noqa: ANN201
    grant = oauth.authorize(project, (fixtures.RESEARCH,), READ_ONLY_SCOPES)
    consent = oauth.consent(grant.id, project)
    return grant, consent, provider.issue_code(parse_qs(urlsplit(consent.url).query)["code_challenge"][0])


# --- Consent and the callback ---------------------------------------------------------------------------------------


def test_a_completed_consent_keeps_only_an_encrypted_refresh_token() -> None:
    oauth, provider, _ = flow()
    grant, consent, code = begin(oauth, provider)
    assert oauth.complete(state=consent.state, code=code, error=None, binding=consent.binding) == (grant.id, "p1")
    stored = oauth.store.get(grant.id)
    refresh = next(t for t in provider.live if t.startswith("sim-refresh-"))
    assert stored is not None and stored.ciphertext.startswith(b"sealed:") and refresh.encode() not in stored.ciphertext and stored.scopes == READ_ONLY_SCOPES
    assert oauth.token(grant.id).startswith("sim-access-") and provider.refreshes == 0
    assert "sim-" not in repr(TokenSet("sim-access-x", 1, DRIVE_READONLY, "sim-refresh-y")) and "sealed" not in repr(stored)


def test_a_forged_state_or_another_browser_is_refused_and_the_real_state_is_spent() -> None:
    oauth, provider, _ = flow()
    _, consent, code = begin(oauth, provider)
    with pytest.raises(DriveAccessRefused, match="unknown, expired or already used state"):
        oauth.complete(state="forged-state", code=code, error=None, binding=consent.binding)
    with pytest.raises(DriveAccessRefused, match="did not come from the browser"):
        oauth.complete(state=consent.state, code=code, error=None, binding="another browser")
    with pytest.raises(DriveAccessRefused, match="already used"):  # a refused answer spends its state: no second try
        oauth.complete(state=consent.state, code=code, error=None, binding=consent.binding)
    assert provider.exchanges == 0


def test_a_callback_is_accepted_once_and_a_replay_is_refused() -> None:
    oauth, provider, _ = flow()
    _, consent, code = begin(oauth, provider)
    oauth.complete(state=consent.state, code=code, error=None, binding=consent.binding)
    with pytest.raises(DriveAccessRefused, match="already used"):
        oauth.complete(state=consent.state, code=code, error=None, binding=consent.binding)
    assert provider.exchanges == 1


def test_an_expired_state_is_refused() -> None:
    oauth, provider, clock = flow()
    _, consent, code = begin(oauth, provider)
    clock.now += 601
    with pytest.raises(DriveAccessRefused, match="expired"):
        oauth.complete(state=consent.state, code=code, error=None, binding=consent.binding)
    assert provider.exchanges == 0


def test_a_code_works_only_with_its_own_pkce_verifier() -> None:
    oauth, provider, _ = flow()
    _, consent, _ = begin(oauth, provider)
    stolen = provider.issue_code("a-challenge-for-some-other-verifier")
    with pytest.raises(DriveAuthorizationRequired, match="did not accept"):
        oauth.complete(state=consent.state, code=stolen, error=None, binding=consent.binding)


def test_a_google_error_or_a_malformed_code_never_grants() -> None:
    oauth, provider, _ = flow()
    _, consent, _ = begin(oauth, provider)
    with pytest.raises(DriveAuthorizationRequired, match="access_denied"):
        oauth.complete(state=consent.state, code=None, error="access_denied", binding=consent.binding)
    _, consent, _ = begin(oauth, provider)
    with pytest.raises(DriveAccessRefused, match="no valid authorization code"):
        oauth.complete(state=consent.state, code="x' or 1=1", error=None, binding=consent.binding)
    assert provider.exchanges == 0


def test_consent_is_handed_out_once_and_only_to_its_own_project() -> None:
    oauth, _, _ = flow()
    grant = oauth.authorize("p1", (fixtures.RESEARCH,), READ_ONLY_SCOPES)
    with pytest.raises(DriveAuthorizationRequired, match="in this project"):
        oauth.consent(grant.id, "another-project")
    oauth.consent(grant.id, "p1")
    with pytest.raises(DriveAuthorizationRequired):
        oauth.consent(grant.id, "p1")


@pytest.mark.parametrize("uri", [REDIRECT, "http://localhost:8765/oauth/google/callback", "https://devpilot.example/oauth/google/callback"])
def test_a_strict_redirect_uri_is_accepted(uri: str) -> None:
    assert check_redirect_uri(uri) == uri


@pytest.mark.parametrize(
    "uri",
    ["http://devpilot.example/oauth/google/callback", "http://127.0.0.1/oauth/google/callback", "https://devpilot.example/oauth/google/callback?next=/admin",
     "https://devpilot.example/oauth/google/callback#x", "https://user:pw@devpilot.example/oauth/google/callback", "https://devpilot.example/other",
     "https://*.devpilot.example/oauth/google/callback", "javascript:alert(1)"],
)  # fmt: skip
def test_an_unsafe_redirect_uri_is_a_configuration_error(uri: str) -> None:
    with pytest.raises(ValueError):
        check_redirect_uri(uri)


# --- Storage, refresh and revocation --------------------------------------------------------------------------------


def test_without_the_approved_cipher_consent_fails_closed_before_any_exchange() -> None:
    cipher, why = build_cipher(SecretStr("not-a-key"))
    assert cipher is None and why is not None and "cannot be stored" in why  # cryptography is not installed here, or the key is invalid
    oauth, provider, _ = flow(cipher=None, problem=why)
    grant, consent, code = begin(oauth, provider)
    with pytest.raises(CredentialStorageUnavailable, match="cannot be stored"):
        oauth.complete(state=consent.state, code=code, error=None, binding=consent.binding)
    assert provider.exchanges == 0 and oauth.store.get(grant.id) is None


def test_a_broader_scope_or_a_missing_refresh_token_is_refused_and_the_tokens_discarded() -> None:
    oauth, provider, _ = flow(scope="https://www.googleapis.com/auth/drive")
    grant, consent, code = begin(oauth, provider)
    with pytest.raises(DriveAccessRefused, match="beyond read-only"):
        oauth.complete(state=consent.state, code=code, error=None, binding=consent.binding)
    assert oauth.store.get(grant.id) is None and provider.revoked
    oauth, provider, _ = flow(give_refresh=False)
    grant, consent, code = begin(oauth, provider)
    with pytest.raises(DriveAccessRefused, match="no refresh token"):
        oauth.complete(state=consent.state, code=code, error=None, binding=consent.binding)
    assert oauth.store.get(grant.id) is None


def test_refresh_rotates_safely_keeps_credentials_on_outage_and_drops_them_when_invalid() -> None:
    oauth, provider, clock = flow(rotate=True)
    grant, consent, code = begin(oauth, provider)
    oauth.complete(state=consent.state, code=code, error=None, binding=consent.binding)
    first = oauth.store.get(grant.id)
    clock.now += 4_000
    assert oauth.token(grant.id).startswith("sim-access-") and provider.refreshes == 1 and oauth.store.get(grant.id) != first  # rotated, stored encrypted again
    clock.now += 4_000
    provider.refresh_error = "unavailable"
    with pytest.raises(DriveUnavailable, match="could not be refreshed"):
        oauth.token(grant.id)
    assert oauth.store.get(grant.id) is not None  # an outage changes nothing
    invalidated: list[str] = []
    oauth.on_invalidated = invalidated.append
    provider.refresh_error = "invalid_grant"
    with pytest.raises(DriveAuthorizationRequired, match="consent must be given again"):
        oauth.token(grant.id)
    assert oauth.store.get(grant.id) is None and invalidated == [grant.id]


def test_concurrent_callers_share_one_refresh() -> None:
    oauth, provider, clock = flow()
    grant, consent, code = begin(oauth, provider)
    oauth.complete(state=consent.state, code=code, error=None, binding=consent.binding)
    clock.now += 4_000
    barrier, tokens = threading.Barrier(6), []
    threads = [threading.Thread(target=lambda: (barrier.wait(), tokens.append(oauth.token(grant.id)))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert provider.refreshes == 1 and len(set(tokens)) == 1


def test_revocation_reports_local_deletion_and_remote_revocation_separately() -> None:
    oauth, provider, _ = flow()
    grant, consent, code = begin(oauth, provider)
    oauth.complete(state=consent.state, code=code, error=None, binding=consent.binding)
    done = oauth.revoke(grant)
    assert (done.local_deleted, done.remote) == (True, "simulated") and oauth.store.get(grant.id) is None
    oauth, provider, _ = flow(revoke_error="failed")
    grant, consent, code = begin(oauth, provider)
    oauth.complete(state=consent.state, code=code, error=None, binding=consent.binding)
    failed = oauth.revoke(grant)
    assert (failed.local_deleted, failed.remote) == (True, "failed") and "myaccount.google.com" in failed.detail and oauth.store.get(grant.id) is None
    with pytest.raises(DriveAuthorizationRequired):
        oauth.token(grant.id)  # gone locally, whatever Google said
    assert oauth.revoke(grant).remote == "not_attempted"


# --- The real provider's HTTP, through an in-process transport only -------------------------------------------------


def test_the_google_provider_sends_the_pkce_verifier_and_maps_errors_without_echoing_secrets() -> None:
    seen: list[dict[str, list[str]]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        seen.append(form)
        if form.get("grant_type") == ["refresh_token"]:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
        if request.url.path == "/revoke":
            return httpx.Response(503)
        return httpx.Response(200, json={"access_token": "a", "expires_in": 3599, "scope": DRIVE_READONLY, "refresh_token": "r"})

    provider = GoogleOAuthProvider(EGRESS, "client-123", SecretStr("test-client-secret"), client=httpx.Client(transport=httpx.MockTransport(answer)))
    tokens = provider.exchange("code-1234567890", "the-verifier", REDIRECT)
    assert tokens.refresh_token == "r" and seen[0]["code_verifier"] == ["the-verifier"] and seen[0]["redirect_uri"] == [REDIRECT]
    with pytest.raises(InvalidGrant) as invalid:
        provider.refresh("r")
    with pytest.raises(ProviderUnavailable):
        provider.revoke("r")
    assert "test-client-secret" not in str(invalid.value)
    blocked = GoogleOAuthProvider(EgressPolicy({"backend": ["api.openai.com"]}), "client-123", None, client=httpx.Client(transport=httpx.MockTransport(answer)))
    with pytest.raises(OAuthError, match="egress policy refused"):
        blocked.exchange("code-1234567890", "v", REDIRECT)
    odd = GoogleOAuthProvider(EGRESS, "client-123", None, client=httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"access_token": "a", "expires_in": "soon"}))))
    with pytest.raises(OAuthError, match="invalid expiry"):
        odd.exchange("code-1234567890", "v", REDIRECT)


# --- The HTTP routes ------------------------------------------------------------------------------------------------


@pytest.fixture
def site(tmp_path):  # noqa: ANN001, ANN201
    def build(**options: object):  # noqa: ANN202
        oauth, provider, clock = flow(**options)  # type: ignore[arg-type]
        runtime = DriveRuntime(GoogleDriveSource(EGRESS, oauth), oauth)
        app = create_app(make_settings(tmp_path / str(len(made)), log_level="ERROR"), llm=FakeLLM(), drive=runtime)
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        made.append(app)
        project = client.post("/api/v1/projects", json={"name": "p", "requirement_text": "A chatbot."}, headers=HEADERS).json()["id"]
        grant = runtime.grants.add(oauth.authorize(project, (fixtures.RESEARCH,), READ_ONLY_SCOPES))
        return client, runtime, provider, project, grant

    made: list = []
    yield build
    for app in made:
        app.state.engine.dispose()


HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}


def _start(client: TestClient, project: str, grant_id: str, provider: fixtures.SimulatedOAuthProvider) -> tuple[str, str]:
    started = client.post("/api/v1/integrations/google/consent", json={"project_id": project, "grant_id": grant_id}, headers=HEADERS)
    assert started.status_code == 200 and BINDING_COOKIE in client.cookies
    query = parse_qs(urlsplit(started.json()["url"]).query)
    return query["state"][0], provider.issue_code(query["code_challenge"][0])


def test_the_callback_grants_once_for_the_browser_that_started_consent(site) -> None:  # noqa: ANN001
    client, runtime, provider, project, grant = site()
    state, code = _start(client, project, grant.id, provider)
    stranger = TestClient(client.app, base_url="http://127.0.0.1:8000")  # no binding cookie
    assert stranger.get(CALLBACK_PATH, params={"state": state, "code": code}).status_code == 400
    state, code = _start(client, project, runtime.grants.add(runtime.authorizer.authorize(project, (fixtures.RESEARCH,), READ_ONLY_SCOPES)).id, provider)
    done = client.get(CALLBACK_PATH, params={"state": state, "code": code})
    assert done.status_code == 200 and "granted" in done.text and BINDING_COOKIE not in client.cookies
    assert any(g.status is GrantStatus.GRANTED for g in runtime.grants.for_project(project))
    assert client.get(CALLBACK_PATH, params={"state": state, "code": code}).status_code == 400  # replayed
    assert "sim-" not in done.text


def test_the_callback_fails_closed_without_the_approved_cipher(site) -> None:  # noqa: ANN001
    client, runtime, provider, project, grant = site(cipher=None, problem="refresh tokens cannot be stored: the approved encryption (cryptography's Fernet) is not installed")
    state, code = _start(client, project, grant.id, provider)
    answer = client.get(CALLBACK_PATH, params={"state": state, "code": code})
    assert answer.status_code == 503 and "cannot be stored" in answer.text and provider.exchanges == 0
    assert runtime.grants.get(grant.id).status is GrantStatus.PENDING_CONSENT


def test_credential_deletion_is_per_project_and_reports_a_failed_remote_revocation(site) -> None:  # noqa: ANN001
    client, runtime, provider, project, grant = site(revoke_error="failed")
    state, code = _start(client, project, grant.id, provider)
    client.get(CALLBACK_PATH, params={"state": state, "code": code})
    other = client.post("/api/v1/projects", json={"name": "q", "requirement_text": "Another."}, headers=HEADERS).json()["id"]
    theirs = runtime.grants.add(runtime.authorizer.authorize(other, (fixtures.RESEARCH,), READ_ONLY_SCOPES))
    answer = client.delete("/api/v1/integrations/google/credentials", params={"project_id": project}, headers=HEADERS).json()
    assert answer["local_deleted"] is True and answer["remote_revoked"] is False and "myaccount.google.com" in answer["note"]
    assert [g["remote_revocation"] for g in answer["grants"]] == ["failed"] and runtime.grants.get(grant.id).status is GrantStatus.REVOKED
    assert runtime.grants.get(theirs.id).status is GrantStatus.PENDING_CONSENT  # another project's authorization is untouched
    assert client.delete("/api/v1/integrations/google/credentials", params={"project_id": "f" * 32}, headers=HEADERS).status_code == 404
    assert client.delete("/api/v1/integrations/google/credentials", params={"project_id": project}).status_code == 401


def test_every_route_is_absent_while_drive_is_off(tmp_path) -> None:  # noqa: ANN001
    app = create_app(make_settings(tmp_path, log_level="ERROR"), llm=FakeLLM())
    client = TestClient(app)
    try:
        assert client.post("/api/v1/integrations/google/consent", json={"project_id": "a" * 32, "grant_id": "grant-" + "0" * 16}, headers=HEADERS).status_code == 404
        assert client.get(CALLBACK_PATH, params={"state": "s", "code": "c"}).status_code == 404
        assert client.delete("/api/v1/integrations/google/credentials", params={"project_id": "a" * 32}, headers=HEADERS).status_code == 404
    finally:
        app.state.engine.dispose()
