"""Google Drive access, offline: authorization states, scopes, DevPilot's folder restriction, pagination, and how the
Drive v3 client handles failures. Google is a fixture transport (app/drive/fixtures.py); nothing here reaches a network."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.config import REPO_ROOT
from app.core.egress import EgressPolicy
from app.drive import fixtures
from app.drive.access import DiscoveryLimits, FixtureAuthorizer, GoogleOAuthAuthorizer, GrantStatus, GrantStore, ScopedDrive, check_scopes
from app.drive.model import DRIVE_READONLY, READ_ONLY_SCOPES, DriveAccessRefused, DriveAuthorizationRequired, DriveUnavailable, folder_id
from app.drive.source import GoogleDriveSource

EGRESS = EgressPolicy.from_file(REPO_ROOT / "config" / "egress_allowlist.yaml")
RESEARCH_FILES = {"fxDocRagNotes001", "fxTxtChunking001", "fxDocxEval000001", "fxPdfSurvey00001", "fxPngDiagram0001", fixtures.POISONED, "fxTxtHuge0000001", "fxDocxBroken0001",
                  "fxDocCitation001"}  # fmt: skip


@pytest.fixture
def drive() -> fixtures.FixtureDrive:
    return fixtures.FixtureDrive.research()


def make(drive: fixtures.FixtureDrive, *, folders: tuple[str, ...] = (fixtures.RESEARCH,), account: str = fixtures.RESEARCHER, project: str = "p1", transport: httpx.BaseTransport | None = None):  # noqa: ANN201
    authorizer = FixtureAuthorizer(fixtures.TOKENS, account)
    source = GoogleDriveSource(EGRESS, authorizer, client=httpx.Client(transport=transport or drive.transport()), sleep=lambda _s: None)
    return source, authorizer, authorizer.authorize(project, folders, READ_ONLY_SCOPES)


# --- Folder IDs and scopes ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [fixtures.RESEARCH, fixtures.FOLDER_URL, f"https://drive.google.com/drive/u/0/folders/{fixtures.RESEARCH}?usp=sharing"])
def test_a_folder_is_named_by_its_id_or_its_drive_link(value: str) -> None:
    assert folder_id(value) == fixtures.RESEARCH


@pytest.mark.parametrize("value", ["research", "x' in parents or '1", "https://docs.google.com/document/d/fxResearchRoot01/edit", "javascript:alert(1)", "../../etc/passwd",
                                   f"https://evil.example/drive/folders/{fixtures.RESEARCH}"])  # fmt: skip
def test_anything_else_is_refused_before_it_can_reach_a_query(value: str) -> None:
    with pytest.raises(DriveAccessRefused):
        folder_id(value)


def test_only_the_read_only_scope_may_be_requested() -> None:
    assert check_scopes([DRIVE_READONLY]) == READ_ONLY_SCOPES
    for scopes in ([], ["https://www.googleapis.com/auth/drive"], ["https://www.googleapis.com/auth/drive.file"], [DRIVE_READONLY, "https://www.googleapis.com/auth/drive.metadata"]):
        with pytest.raises(DriveAccessRefused, match="read-only"):
            check_scopes(scopes)


# --- Authorization states -------------------------------------------------------------------------------------------


def test_a_grant_belongs_to_one_project_and_ends_when_revoked(drive: fixtures.FixtureDrive) -> None:
    source, authorizer, grant = make(drive)
    store = GrantStore()
    store.add(grant)
    assert grant.status is GrantStatus.GRANTED and grant.scopes == READ_ONLY_SCOPES and grant.folders == (fixtures.RESEARCH,) and grant.simulated
    assert store.usable(grant.id, "p1") is grant
    with pytest.raises(DriveAuthorizationRequired, match="for this project"):
        store.usable(grant.id, "another-project")
    store.revoke(grant.id, "p1")
    authorizer.revoke(grant)
    with pytest.raises(DriveAuthorizationRequired, match="revoked"):
        store.usable(grant.id, "p1")
    with pytest.raises(DriveAuthorizationRequired):
        source.get(grant.id, fixtures.RESEARCH)  # the token is gone too


def test_the_real_consent_flow_builds_a_safe_url_and_never_grants() -> None:
    oauth = GoogleOAuthAuthorizer("client-123.apps.googleusercontent.com", "http://127.0.0.1:8000/oauth/google/callback")
    grant = oauth.authorize("p1", (fixtures.RESEARCH,), READ_ONLY_SCOPES)
    query = parse_qs(urlsplit(oauth.pending[grant.id].url).query)
    assert grant.status is GrantStatus.PENDING_CONSENT and not grant.simulated
    assert query["scope"] == [DRIVE_READONLY] and query["code_challenge_method"] == ["S256"] and query["state"][0] and query["response_type"] == ["code"]
    assert "client_secret" not in query and oauth.pending[grant.id].code_verifier not in oauth.pending[grant.id].url
    with pytest.raises(DriveAuthorizationRequired, match="not implemented"):
        oauth.token(grant.id)
    with pytest.raises(DriveAuthorizationRequired, match="GOOGLE_OAUTH_CLIENT_ID"):
        GoogleOAuthAuthorizer(None, "http://127.0.0.1:8000/cb").authorize("p1", (fixtures.RESEARCH,), READ_ONLY_SCOPES)


# --- DevPilot's folder restriction ----------------------------------------------------------------------------------


def test_discovery_reaches_the_approved_folder_and_its_subfolders_only(drive: fixtures.FixtureDrive) -> None:
    source, _, grant = make(drive)
    found = ScopedDrive(source, grant, DiscoveryLimits()).discover()
    assert set(found.files) == RESEARCH_FILES and set(found.folders) == {fixtures.RESEARCH, fixtures.PAPERS} and not found.truncated
    asked = " ".join(p.get("q", "") + path for path, p in drive.calls)
    assert fixtures.PERSONAL not in asked and fixtures.COLLEAGUE_FOLDER not in asked  # never even asked about


def test_pages_are_followed_and_limits_are_reported(drive: fixtures.FixtureDrive) -> None:
    source, _, grant = make(drive)
    paged = ScopedDrive(source, grant, DiscoveryLimits(page_size=2)).discover()
    assert set(paged.files) == RESEARCH_FILES and paged.pages > 4
    few = ScopedDrive(source, grant, DiscoveryLimits(max_files=3)).discover()
    assert len(few.files) == 3 and few.truncated
    shallow = ScopedDrive(source, grant, DiscoveryLimits(max_depth=0)).discover()
    assert "fxDocCitation001" not in shallow.files and shallow.truncated and fixtures.PAPERS not in shallow.folders


def test_another_accounts_folder_cannot_be_an_approved_folder(drive: fixtures.FixtureDrive) -> None:
    source, _, grant = make(drive, folders=(fixtures.COLLEAGUE_FOLDER,))
    with pytest.raises(DriveUnavailable, match="404"):
        ScopedDrive(source, grant, DiscoveryLimits()).roots()


def test_a_file_must_be_discovered_and_still_inside_the_folders_to_be_read(drive: fixtures.FixtureDrive) -> None:
    source, _, grant = make(drive)
    scoped = ScopedDrive(source, grant, DiscoveryLimits())
    found = scoped.discover()
    meta, data = scoped.fetch("fxTxtChunking001", found, max_bytes=100_000)
    assert meta.kind == "txt" and data.startswith(b"Chunking guidelines")
    with pytest.raises(DriveAccessRefused, match="not discovered"):
        scoped.fetch("fxTxtTaxes00001", found, max_bytes=100_000)  # it exists, and the scope would allow it
    drive.move("fxDocRagNotes001", fixtures.PERSONAL)
    with pytest.raises(DriveAccessRefused, match="no longer in an approved folder"):
        scoped.fetch("fxDocRagNotes001", found, max_bytes=100_000)
    drive.trash("fxDocCitation001")
    with pytest.raises(DriveUnavailable, match="deleted"):
        scoped.fetch("fxDocCitation001", found, max_bytes=100_000)
    with pytest.raises(DriveAccessRefused, match="not a supported document type"):
        scoped.fetch("fxPngDiagram0001", found, max_bytes=100_000)


def test_an_oversized_file_is_refused_before_it_is_downloaded(drive: fixtures.FixtureDrive) -> None:
    source, _, grant = make(drive)
    scoped = ScopedDrive(source, grant, DiscoveryLimits())
    found = scoped.discover()
    with pytest.raises(DriveAccessRefused, match="over the"):
        scoped.fetch("fxTxtHuge0000001", found, max_bytes=10 * 1024 * 1024)
    assert not [p for path, p in drive.calls if p.get("alt") == "media"]
    with pytest.raises(DriveAccessRefused, match="byte limit"):  # and a stream that runs past the limit is cut
        scoped.fetch("fxDocxEval000001", found, max_bytes=100)


# --- The Drive v3 client's failures ---------------------------------------------------------------------------------


def test_rate_limits_and_server_errors_are_retried_a_bounded_number_of_times(drive: fixtures.FixtureDrive) -> None:
    source, _, grant = make(drive)
    drive.fail_next = [(503, "backendError"), (403, "userRateLimitExceeded")]
    assert source.get(grant.id, fixtures.RESEARCH).is_folder
    drive.fail_next = [(500, "backendError")] * 3
    with pytest.raises(DriveUnavailable, match="after 3 tries"):
        source.get(grant.id, fixtures.RESEARCH)
    drive.fail_next = [(403, "insufficientFilePermissions"), (200, "")]
    with pytest.raises(DriveUnavailable, match="403"):  # a refusal is not retried
        source.get(grant.id, fixtures.RESEARCH)


def test_a_revoked_token_is_an_authorization_failure_and_no_error_holds_the_token(drive: fixtures.FixtureDrive) -> None:
    source, _, grant = make(drive)
    drive.forget_token(fixtures.RESEARCHER)
    with pytest.raises(DriveAuthorizationRequired) as raised:
        source.get(grant.id, fixtures.RESEARCH)
    assert "fixture-token" not in str(raised.value)


def test_a_redirect_off_the_allowlist_is_refused_before_it_is_followed(drive: fixtures.FixtureDrive) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.googleapis.com":
            return httpx.Response(302, headers={"location": "https://doc-0s-1c-docs.googleusercontent.com/file"})
        raise AssertionError("the redirect was followed")

    source, _, grant = make(drive, transport=httpx.MockTransport(answer))
    with pytest.raises(DriveAccessRefused, match="egress policy refused"):
        source.download(grant.id, "fxTxtChunking001", max_bytes=1_000)


def test_an_unsafe_id_never_reaches_a_request(drive: fixtures.FixtureDrive) -> None:
    source, _, grant = make(drive)
    for bad in ("x' in parents or '1", "../files", "short"):
        with pytest.raises(DriveAccessRefused):
            source.children(grant.id, bad, page_token=None, page_size=10)
    assert drive.calls == []


def test_the_fixture_documents_are_invented_and_served_in_process() -> None:
    drive = fixtures.FixtureDrive.research()
    assert all(i.owner.endswith("@example.test") for i in drive.items.values()) and isinstance(drive.transport(), httpx.MockTransport)
