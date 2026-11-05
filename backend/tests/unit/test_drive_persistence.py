"""Google Drive consent survives a restart: grants and encrypted refresh tokens on disk (app/drive/persistent.py)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.core.egress import EgressPolicy
from app.drive import fixtures
from app.drive.access import Grant, GrantStatus
from app.drive.model import READ_ONLY_SCOPES
from app.drive.oauth import CredentialStorageUnavailable, FernetCipher, GoogleOAuthAuthorizer, StoredCredential
from app.drive.persistent import FileCredentialStore, FileGrantStore
from app.tools.adapters.drive import build_drive_runtime
from tests.helpers import ALLOWLIST_FILE, make_settings

REDIRECT = "http://127.0.0.1:8000/oauth/google/callback"
GRANT = "grant-00000000000000a1"


def authorizer(directory: Path, provider: fixtures.SimulatedOAuthProvider, key: SecretStr) -> GoogleOAuthAuthorizer:
    return GoogleOAuthAuthorizer("client-123.apps.googleusercontent.com", REDIRECT, provider=provider, cipher=FernetCipher(key), store=FileCredentialStore(directory))


def test_only_ciphertext_reaches_the_disk_and_it_is_read_back_after_a_restart(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path / "drive")
    store.put(GRANT, StoredCredential("p1", b"gAAAAA-ciphertext", READ_ONLY_SCOPES, 1.0))
    again = FileCredentialStore(tmp_path / "drive")
    assert again.get(GRANT) == StoredCredential("p1", b"gAAAAA-ciphertext", READ_ONLY_SCOPES, 1.0)
    assert again.delete(GRANT) and not (tmp_path / "drive" / f"{GRANT}.json").exists() and FileCredentialStore(tmp_path / "drive").get(GRANT) is None
    with pytest.raises(CredentialStorageUnavailable):
        store.put(GRANT, StoredCredential("p1", b"", READ_ONLY_SCOPES, 1.0))
    with pytest.raises(ValueError):
        store.put("../escape", StoredCredential("p1", b"x", READ_ONLY_SCOPES, 1.0))


def test_grants_and_their_status_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "drive-grants.json"
    grants = FileGrantStore(path)
    grants.add(Grant(GRANT, "p1", ("folder-1", "file-2"), READ_ONLY_SCOPES, GrantStatus.PENDING_CONSENT, False, "pending", "2026-11-05"))
    grants.activate(GRANT, "p1")
    assert FileGrantStore(path).usable(GRANT, "p1").folders == ("folder-1", "file-2")
    grants.revoke(GRANT, "p1")
    restored = FileGrantStore(path).get(GRANT)
    assert restored is not None and restored.status is GrantStatus.REVOKED and restored.revoked_at
    assert "token" not in path.read_text(encoding="utf-8").lower()  # no credential in the grant record


def test_after_a_restart_the_stored_refresh_token_still_gives_access(tmp_path: Path) -> None:
    key = SecretStr(Fernet.generate_key().decode())
    provider = fixtures.SimulatedOAuthProvider()
    first = authorizer(tmp_path / "drive", provider, key)
    grant = first.authorize("p1", (fixtures.RESEARCH,), READ_ONLY_SCOPES)
    consent = first.consent(grant.id, "p1")
    code = provider.issue_code(parse_qs(urlsplit(consent.url).query)["code_challenge"][0])
    first.complete(state=consent.state, code=code, error=None, binding=consent.binding)
    on_disk = (tmp_path / "drive" / f"{grant.id}.json").read_text(encoding="utf-8")
    issued = [t for t in provider.live if t.startswith("sim-refresh-")]
    assert issued and all(token not in on_disk for token in issued)  # the refresh token is on disk only as ciphertext
    restarted = authorizer(tmp_path / "drive", provider, key)  # a new process: nothing in memory
    before = provider.refreshes
    assert restarted.token(grant.id) and provider.refreshes == before + 1  # access comes from the stored, encrypted refresh token
    with pytest.raises(CredentialStorageUnavailable):
        authorizer(tmp_path / "drive", provider, SecretStr(Fernet.generate_key().decode())).token(grant.id)  # another key cannot read it


def test_the_real_drive_runtime_keeps_its_consent_under_the_credentials_folder(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, drive_source="google", google_oauth_client_id="client.apps.googleusercontent.com", credentials_dir=tmp_path / "credentials",
                             devpilot_token_key=SecretStr(Fernet.generate_key().decode()))  # fmt: skip
    runtime = build_drive_runtime(settings, EgressPolicy.from_file(ALLOWLIST_FILE))
    assert runtime is not None and isinstance(runtime.grants, FileGrantStore) and isinstance(runtime.authorizer.store, FileCredentialStore)
    runtime.grants.add(Grant(GRANT, "p1", ("folder-1",), READ_ONLY_SCOPES, GrantStatus.PENDING_CONSENT, False, "pending", "2026-11-05"))
    assert (tmp_path / "credentials" / "drive-grants.json").is_file()
    assert build_drive_runtime(settings, EgressPolicy.from_file(ALLOWLIST_FILE)).grants.get(GRANT) is not None  # type: ignore[union-attr]


# --- Knowledge bases after a restart ------------------------------------------------------------------------------


def restarted_runtime(tmp_path: Path, project: str):  # noqa: ANN201
    """A Drive runtime as a new process builds it: an active grant on disk and the persistent index, nothing in memory."""
    from app.demo import rag as rag_demo
    from app.rag.embeddings import HashingEmbedder
    from app.rag.sqlite_index import SqliteVectorIndex

    settings = make_settings(tmp_path)
    runtime, _source = rag_demo.fixture_runtime(fixtures.FixtureDrive.research(), settings)
    runtime.grants = FileGrantStore(tmp_path / "drive-grants.json")
    runtime.embedders = (HashingEmbedder(),)
    runtime.index_factory = lambda e: SqliteVectorIndex(tmp_path / "rag" / "index.sqlite3", e.name, e.dimensions)
    return runtime


def test_after_a_restart_the_knowledge_base_comes_back_and_revocation_still_deletes_its_chunks(tmp_path: Path) -> None:
    from app.rag.extract import ExtractedDocument, Section
    from app.rag.knowledge import KnowledgeBase

    first = restarted_runtime(tmp_path, "p1")
    first.grants.add(Grant(GRANT, "p1", ("folder-1",), READ_ONLY_SCOPES, GrantStatus.GRANTED, True, "fixture", "2026-11-05"))
    embedder = first.embedders[0]
    base = KnowledgeBase(embedder, index=first.index_factory(embedder))
    base.add(ExtractedDocument("file-1", "Leave policy", "text", "t", GRANT, "h1", (Section("paragraph 1", "Staff get 25 days of paid holiday each year."),), ()), owner="p1")

    second = restarted_runtime(tmp_path, "p1")  # a restart: no knowledge base in memory
    restored = second.base_for("p1")
    assert restored is not None and restored.retrieve("paid holiday days", second.scope("p1"), k=1, min_score=0.0)
    assert second.base_for("p2") is None  # another project sees nothing

    third = restarted_runtime(tmp_path, "p1")  # a restart, then revocation before anyone asked anything
    third.grants.revoke(GRANT, "p1")
    _documents, chunks = third.forget("p1", GRANT)
    assert chunks >= 1
    from app.rag.index import AccessScope

    on_disk = restarted_runtime(tmp_path, "p1").index_factory(embedder)
    assert on_disk.chunks(AccessScope("p1", frozenset({GRANT}))) == []  # gone from the persistent index, not only from memory
