"""Who may read what from Google Drive: authorizations (grants), and DevPilot's own folder restriction.

A grant belongs to one project, names the folders a person approved, and carries only the read-only scope. That scope
lets Google answer for any file the account can read, so the scope is never trusted to limit access: `ScopedDrive`
reaches a file only by walking down from an approved folder, and checks again, before every download, that the file is
still in one. A file ID GPT invents, a file in another folder, or one moved out since discovery is refused.

Two authorizers:
- `FixtureAuthorizer`: a **simulated** consent for an account of the fixture Drive (tests and the offline
  demonstration). It grants at once, because the person's approval in the approval queue stands for Google's consent.
- `GoogleOAuthAuthorizer`: builds the real consent URL (PKCE, state, read-only scope) and stops there. **The code
  exchange is not implemented in this round, so it never grants**: nothing logs in to Google.

A token lives only inside the authorizer. Grants, events and tool results hold its grant ID, never the token.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlencode

from app.drive.model import READ_ONLY_SCOPES, DriveAccessRefused, DriveAuthorizationRequired, DriveFile, DriveUnavailable, check_id
from app.drive.source import DriveSource


class GrantStatus(StrEnum):
    PENDING_CONSENT = "PENDING_CONSENT"  # a person must still complete Google's consent
    GRANTED = "GRANTED"
    REVOKED = "REVOKED"


@dataclass(frozen=True)
class Grant:
    id: str
    project_id: str  # the only project that may use it
    folders: tuple[str, ...]  # the folders a person approved; nothing outside them is read
    scopes: frozenset[str]
    status: GrantStatus
    simulated: bool
    account: str  # a label for the Google account, never a credential
    created_at: str
    revoked_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"grant_id": self.id, "status": self.status.value, "folders": list(self.folders), "scopes": sorted(self.scopes), "simulated": self.simulated, "account": self.account,
                "created_at": self.created_at, "revoked_at": self.revoked_at}  # fmt: skip


def check_scopes(scopes: Iterable[str]) -> frozenset[str]:
    """Only the read-only scope. Writing, deleting and sharing are never asked for."""
    wanted = frozenset(scopes)
    extra = sorted(wanted - READ_ONLY_SCOPES)
    if extra or not wanted:
        raise DriveAccessRefused(f"only the read-only Drive scope may be requested; refused: {', '.join(extra) or 'no scope'}")
    return wanted


class GrantStore:
    """Grants by ID. A grant is handed out only to the project it belongs to."""

    def __init__(self) -> None:
        self._grants: dict[str, Grant] = {}

    def add(self, grant: Grant) -> Grant:
        self._grants[grant.id] = grant
        return grant

    def usable(self, grant_id: str, project_id: str) -> Grant:
        grant = self._grants.get(grant_id)
        if grant is None or grant.project_id != project_id:
            raise DriveAuthorizationRequired("there is no Google Drive authorization with that ID for this project")
        if grant.status is not GrantStatus.GRANTED:
            raise DriveAuthorizationRequired(f"the Google Drive authorization is {grant.status.value.lower().replace('_', ' ')}")
        return grant

    def revoke(self, grant_id: str, project_id: str) -> Grant:
        grant = self._grants.get(grant_id)
        if grant is None or grant.project_id != project_id:
            raise DriveAuthorizationRequired("there is no Google Drive authorization with that ID for this project")
        revoked = replace(grant, status=GrantStatus.REVOKED, revoked_at=grant.revoked_at or _now())
        self._grants[grant_id] = revoked
        return revoked

    def for_project(self, project_id: str) -> list[Grant]:
        return [g for g in self._grants.values() if g.project_id == project_id]


class Authorizer(Protocol):
    name: str
    simulated: bool

    def authorize(self, project_id: str, folders: tuple[str, ...], scopes: frozenset[str]) -> Grant: ...

    def revoke(self, grant: Grant) -> None:
        """Forget the grant's token. After this, every call with the grant gets DriveAuthorizationRequired."""
        ...

    def token(self, grant_id: str) -> str: ...


class FixtureAuthorizer:
    """SIMULATED consent for one account of the fixture Drive. The token is the fixture account's, never a real one."""

    name = "fixture-consent"
    simulated = True

    def __init__(self, account_tokens: Mapping[str, str], account: str) -> None:
        self._account_tokens = dict(account_tokens)
        self._account = account
        self._tokens: dict[str, str] = {}
        self._issued = itertools.count(1)  # predictable IDs, so a scripted run can name its grant; real grants are random

    def authorize(self, project_id: str, folders: tuple[str, ...], scopes: frozenset[str]) -> Grant:
        grant = Grant(f"grant-{next(self._issued):016x}", project_id, folders, check_scopes(scopes), GrantStatus.GRANTED, True, self._account, _now())
        self._tokens[grant.id] = self._account_tokens[self._account]
        return grant

    def revoke(self, grant: Grant) -> None:
        self._tokens.pop(grant.id, None)

    def token(self, grant_id: str) -> str:
        token = self._tokens.get(grant_id)
        if token is None:
            raise DriveAuthorizationRequired("the Google Drive authorization was revoked or never completed")
        return token


@dataclass(frozen=True)
class ConsentRequest:
    """What a person would open to give consent. Kept by DevPilot; GPT is told only that consent is pending."""

    url: str
    state: str
    code_verifier: str


class GoogleOAuthAuthorizer:
    """The real flow's first half: a consent URL with PKCE (S256), a state value and the read-only scope.

    NOT IMPLEMENTED in this round: the redirect handler and the code exchange at oauth2.googleapis.com, token storage
    and refresh, and the call to Google's revocation endpoint. So `authorize` always returns a pending grant."""

    name = "google-oauth"
    simulated = False
    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

    def __init__(self, client_id: str | None, redirect_uri: str) -> None:
        self._client_id = client_id
        self._redirect_uri = redirect_uri
        self.pending: dict[str, ConsentRequest] = {}

    def consent_request(self, scopes: frozenset[str]) -> ConsentRequest:
        if not self._client_id:
            raise DriveAuthorizationRequired("GOOGLE_OAUTH_CLIENT_ID is not configured")
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        state = secrets.token_urlsafe(24)
        query = {"client_id": self._client_id, "redirect_uri": self._redirect_uri, "response_type": "code", "scope": " ".join(sorted(check_scopes(scopes))), "state": state,
                 "code_challenge": challenge, "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent", "include_granted_scopes": "false"}  # fmt: skip
        return ConsentRequest(f"{self.AUTH_URL}?{urlencode(query)}", state, verifier)

    def authorize(self, project_id: str, folders: tuple[str, ...], scopes: frozenset[str]) -> Grant:
        request = self.consent_request(scopes)
        grant = Grant(f"grant-{secrets.token_hex(8)}", project_id, folders, scopes, GrantStatus.PENDING_CONSENT, False, "pending", _now())
        self.pending[grant.id] = request
        return grant

    def revoke(self, grant: Grant) -> None:
        self.pending.pop(grant.id, None)

    def token(self, grant_id: str) -> str:
        raise DriveAuthorizationRequired("Google consent was not completed: the real OAuth code exchange is not implemented yet")


# --- DevPilot's own folder restriction ------------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryLimits:
    max_files: int = 200
    max_depth: int = 3  # folders below an approved one
    page_size: int = 100
    max_pages: int = 50  # per folder


@dataclass(frozen=True)
class Discovery:
    folders: dict[str, DriveFile]  # every folder reached, approved roots included
    files: dict[str, DriveFile]  # every non-folder file reached, by ID
    pages: int
    truncated: bool  # stopped at a limit: more files may exist
    notes: list[str] = field(default_factory=list)


class ScopedDrive:
    """One grant's view of Drive: only what lies under its approved folders."""

    def __init__(self, source: DriveSource, grant: Grant, limits: DiscoveryLimits) -> None:
        if grant.status is not GrantStatus.GRANTED:
            raise DriveAuthorizationRequired("the Google Drive authorization is not granted")
        self._source = source
        self._grant = grant
        self._limits = limits

    def roots(self) -> dict[str, DriveFile]:
        """Each approved folder, checked: it exists for this account, is a folder, and is not in the bin."""
        found = {}
        for folder in self._grant.folders:
            meta = self._source.get(self._grant.id, check_id(folder))
            if not meta.is_folder or meta.trashed:
                raise DriveAccessRefused(f"{folder} is not a folder that can be read")
            found[folder] = meta
        return found

    def discover(self) -> Discovery:
        """Breadth-first from the approved folders. A file counts only when it was listed as a child of a folder reached
        this way and names that folder as a parent, so membership comes from the walk, not from what a file claims."""
        limits = self._limits
        folders = self.roots()
        files: dict[str, DriveFile] = {}
        queue = [(folder, 0) for folder in folders]
        pages, truncated, notes = 0, False, []
        while queue:
            folder, depth = queue.pop(0)
            token: str | None = None
            for _ in range(limits.max_pages):
                page = self._source.children(self._grant.id, folder, page_token=token, page_size=limits.page_size)
                pages += 1
                for item in page.files:
                    if folder not in item.parents or item.trashed:
                        continue
                    if item.is_folder:
                        if item.id in folders:
                            continue
                        if depth + 1 > limits.max_depth:
                            notes.append(f"a folder deeper than {limits.max_depth} levels was not opened")
                            truncated = True
                            continue
                        folders[item.id] = item
                        queue.append((item.id, depth + 1))
                    elif len(files) >= limits.max_files:
                        truncated = True
                    else:
                        files.setdefault(item.id, item)
                token = page.next_page_token
                if token is None:
                    break
            else:
                truncated = True
                notes.append(f"a folder had more than {limits.max_pages} pages")
        return Discovery(folders, files, pages, truncated, notes)

    def fetch(self, file_id: str, discovery: Discovery, *, max_bytes: int) -> tuple[DriveFile, bytes]:
        """One discovered file's content, after checking again that it is still under an approved folder."""
        known = discovery.files.get(check_id(file_id))
        if known is None:
            raise DriveAccessRefused("that file was not discovered under an approved folder")
        current = self._source.get(self._grant.id, file_id)
        if current.trashed:
            raise DriveUnavailable("the file was deleted")
        if not set(current.parents) & set(discovery.folders):
            raise DriveAccessRefused("the file is no longer in an approved folder")
        if current.kind is None:
            raise DriveAccessRefused(f"{current.mime_type} is not a supported document type")
        if current.size is not None and current.size > max_bytes:
            raise DriveAccessRefused(f"the file is {current.size} bytes, over the {max_bytes}-byte limit")
        if current.kind == "google-doc":
            return current, self._source.export_text(self._grant.id, file_id, max_bytes=max_bytes)
        return current, self._source.download(self._grant.id, file_id, max_bytes=max_bytes)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
