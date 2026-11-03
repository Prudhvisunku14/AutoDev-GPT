"""Google Drive data as DevPilot sees it, and the ways a Drive call can end. Nothing here talks to Google."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.repos.evaluate import instruction_findings

FOLDER = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TXT = "text/plain"
KINDS = {GOOGLE_DOC: "google-doc", PDF: "pdf", DOCX: "docx", TXT: "txt"}  # the only formats ingested

# The one scope DevPilot asks for. It lets Google answer for every file the account can read, so it never limits access
# to a folder: DevPilot enforces the approved folders itself (access.ScopedDrive). No write, delete or sharing scope.
DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
READ_ONLY_SCOPES = frozenset({DRIVE_READONLY})

# Drive IDs are letters, digits, - and _. Checked before an ID is put in a URL or a files.list query, so no quote or
# operator can reach the query.
FILE_ID = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
_UNSAFE = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})  # control, format (bidi overrides), surrogate, private, unassigned, separators
_FOLDER_URL = re.compile(r"^https://drive\.google\.com/drive/(?:u/\d{1,2}/)?folders/([A-Za-z0-9_-]{10,128})(?:[/?#][^\s]*)?$")


class DriveError(Exception):
    """The message is safe to show to GPT and in logs: it never holds a token or a document's text."""


class DriveUnavailable(DriveError):
    """Google did not answer usefully: not found, not shared, rate limited after retries, or unreadable."""


class DriveAccessRefused(DriveError):
    """DevPilot's own policy refused the request before anything was sent. Never retried."""


class DriveAuthorizationRequired(DriveError):
    """There is no usable authorization: never given, still waiting for consent, revoked, or for another project."""


def folder_id(value: str) -> str:
    """A folder's ID from the ID itself or its Drive URL. Anything else is refused."""
    text = value.strip()
    if FILE_ID.fullmatch(text):
        return text
    match = _FOLDER_URL.fullmatch(text)
    if match:
        return match.group(1)
    raise DriveAccessRefused(f"{text[:80]!r} is not a Google Drive folder ID or folder link")


def safe_label(name: str, limit: int = 120) -> tuple[str, list[str]]:
    """An untrusted name as a short, printable label, with what was done to it. Control, format and bidirectional
    characters are removed (so "doc\\u202eexe.pdf" cannot pose as another name), whitespace is collapsed, and text
    addressed to AI agents is flagged. It stays a label: it is never followed and never authorizes anything."""
    text = unicodedata.normalize("NFC", str(name))
    unsafe = [ch for ch in text if unicodedata.category(ch) in _UNSAFE and ch != " "]
    flags = ["control or invisible characters were removed"] if unsafe else []
    label = " ".join("".join(" " if unicodedata.category(ch) in _UNSAFE else ch for ch in text).split())
    if len(label) > limit:
        label, flags = label[: limit - 1] + "…", [*flags, "shortened"]
    if instruction_findings(label):
        flags.append("the name contains text addressed to AI agents")
    return label, flags


def check_id(file_id: str) -> str:
    if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
        raise DriveAccessRefused("that is not a Google Drive file ID")
    return file_id


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str  # untrusted: whoever owns the file chose it
    mime_type: str
    parents: tuple[str, ...]
    modified_time: str
    size: int | None  # None for Google Docs, which Drive does not size
    trashed: bool = False
    md5: str | None = None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER

    @property
    def kind(self) -> str | None:
        return KINDS.get(self.mime_type)

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "name": self.name, "mime_type": self.mime_type, "kind": self.kind, "parents": list(self.parents), "modified_time": self.modified_time, "size": self.size}


@dataclass(frozen=True)
class Page:
    files: tuple[DriveFile, ...]
    next_page_token: str | None
