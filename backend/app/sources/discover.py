"""GitHub repositories and Google Drive files and folders named in a natural-language request.

Pure parsing: nothing is fetched. Each link is validated before it is reported: GitHub links by `repos.github_url` (exact
host, no credentials, no query tricks), Drive links by exact host and a plain ID. A link that looks like one but is refused
is reported as a problem with the reason, never silently dropped and never echoed with credentials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from app.repos.github_url import _IN_TEXT as GITHUB_IN_TEXT
from app.repos.github_url import RepoRef, RepoUrlError, parse_repo_url

DriveKind = Literal["folder", "file", "document", "spreadsheet", "presentation"]
_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_DRIVE_IN_TEXT = re.compile(r"https?://[^\s<>\"'`]*(?:drive|docs)\.google\.[^\s<>\"'`]*", re.IGNORECASE)
_DRIVE_HOSTS = {"drive.google.com", "docs.google.com"}
_DOCS = {"document": "document", "spreadsheets": "spreadsheet", "presentation": "presentation"}
_DOCS_PATH = {kind: path for path, kind in _DOCS.items()}
MAX_RESOURCES = 10


@dataclass(frozen=True)
class GitHubResource:
    repo: RepoRef
    kind: Literal["github"] = "github"

    @property
    def key(self) -> str:
        return f"github:{self.repo.full_name.lower()}@{self.repo.ref or ''}"

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "key": self.key, "url": self.repo.url, "full_name": self.repo.full_name, "ref": self.repo.ref, "label": str(self.repo)}


@dataclass(frozen=True)
class DriveResource:
    drive_id: str
    drive_type: DriveKind
    kind: Literal["drive"] = "drive"

    @property
    def key(self) -> str:
        return f"drive:{self.drive_id}"

    @property
    def url(self) -> str:
        if self.drive_type == "folder":
            return f"https://drive.google.com/drive/folders/{self.drive_id}"
        if self.drive_type == "file":
            return f"https://drive.google.com/file/d/{self.drive_id}"
        return f"https://docs.google.com/{_DOCS_PATH[self.drive_type]}/d/{self.drive_id}"

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "key": self.key, "url": self.url, "drive_id": self.drive_id, "drive_type": self.drive_type, "label": f"Google Drive {self.drive_type} {self.drive_id[:8]}…"}


Resource = GitHubResource | DriveResource


def parse_drive_url(url: str) -> DriveResource:
    """A Drive folder or file link, or a Docs, Sheets or Slides link, reduced to its ID. Anything else is refused."""
    parts = urlsplit(url.rstrip(".,;:!?)]}"))
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in _DRIVE_HOSTS or parts.port is not None or parts.username or parts.password:
        raise RepoUrlError("only https://drive.google.com and https://docs.google.com links are read")
    segments = [s for s in parts.path.split("/") if s]
    found: tuple[str, DriveKind] | None = None
    if host == "drive.google.com":
        if "folders" in segments and segments.index("folders") + 1 < len(segments):
            found = (segments[segments.index("folders") + 1], "folder")
        elif segments[:1] == ["file"] and len(segments) >= 3 and segments[1] == "d":
            found = (segments[2], "file")
        elif segments[:1] == ["open"]:
            ids = parse_qs(parts.query).get("id", [])
            found = (ids[0], "file") if len(ids) == 1 else None
    elif len(segments) >= 3 and segments[0] in _DOCS and segments[1] == "d":
        found = (segments[2], _DOCS[segments[0]])  # type: ignore[assignment]
    if found is None or not _DRIVE_ID.fullmatch(found[0]):
        raise RepoUrlError("this Google Drive link does not name a file or folder ID DevPilot can read")
    return DriveResource(found[0], found[1])


@dataclass(frozen=True)
class Discovery:
    resources: tuple[Resource, ...]
    problems: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"resources": [r.to_dict() for r in self.resources], "problems": list(self.problems)}


def discover(text: str) -> Discovery:
    resources: dict[str, Resource] = {}
    problems: list[str] = []
    for match in GITHUB_IN_TEXT.finditer(text):
        raw = match.group(0).rstrip(".,;:!?)]}'\"")
        try:
            found: Resource = GitHubResource(parse_repo_url(raw))
        except RepoUrlError as exc:
            problems.append(f"GitHub link refused: {exc}")
            continue
        resources.setdefault(found.key, found)
    for match in _DRIVE_IN_TEXT.finditer(text):
        try:
            drive = parse_drive_url(match.group(0))
        except RepoUrlError as exc:
            problems.append(f"Google Drive link refused: {exc}")
            continue
        resources.setdefault(drive.key, drive)
    found_list = tuple(resources.values())
    if len(found_list) > MAX_RESOURCES:
        problems.append(f"only the first {MAX_RESOURCES} links are read")
    return Discovery(found_list[:MAX_RESOURCES], tuple(dict.fromkeys(problems)))
