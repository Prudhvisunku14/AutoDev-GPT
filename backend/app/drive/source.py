"""Where Drive data comes from: the Google Drive v3 API, through `GoogleDriveSource`.

Every URL it would fetch, redirects included, must first pass the egress allowlist's `google_drive` component, every
answer has a size limit, and an ID is checked before it is put in a URL or a `files.list` query. Rate limits and server
errors are retried a bounded number of times; a 401 means the authorization is gone. The access token comes from a
`TokenProvider` per call and is never logged, recorded or shown to GPT.

**It has never reached Google.** Its tests and the offline demonstration give it an in-process transport serving
Drive-shaped answers (app/drive/fixtures.py), so they exercise this code with no network call.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlencode, urljoin

import httpx

from app.core.egress import EgressPolicy
from app.drive.model import DriveAccessRefused, DriveAuthorizationRequired, DriveFile, DriveUnavailable, Page, check_id

_FIELDS = "id,name,mimeType,parents,modifiedTime,size,trashed,md5Checksum"
_RETRIED = frozenset({429, 500, 502, 503, 504})
_RATE_LIMITED = ("rateLimitExceeded", "userRateLimitExceeded")
_MAX_REDIRECTS = 3


class TokenProvider(Protocol):
    def token(self, grant_id: str) -> str:
        """The grant's current access token. Raises DriveAuthorizationRequired when there is none."""
        ...


class DriveSource(Protocol):
    name: str

    def get(self, grant_id: str, file_id: str) -> DriveFile: ...

    def children(self, grant_id: str, folder_id: str, *, page_token: str | None, page_size: int) -> Page: ...

    def export_text(self, grant_id: str, file_id: str, *, max_bytes: int) -> bytes:
        """A Google Doc as plain text."""
        ...

    def download(self, grant_id: str, file_id: str, *, max_bytes: int) -> bytes:
        """A stored file's bytes (PDF, DOCX, TXT)."""
        ...


class GoogleDriveSource:
    name = "google-drive"
    API = "https://www.googleapis.com/drive/v3"

    def __init__(
        self, egress: EgressPolicy, tokens: TokenProvider, *, client: httpx.Client | None = None, timeout: float = 20.0, max_json_bytes: int = 2_000_000,
        retries: int = 2, backoff_seconds: float = 0.5, sleep: Callable[[float], None] = time.sleep,
    ) -> None:  # fmt: skip
        self._egress = egress
        self._tokens = tokens
        self._client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "DevPilot-AI"})
        self._max_json = max_json_bytes
        self._retries = retries
        self._backoff = backoff_seconds
        self._sleep = sleep

    def get(self, grant_id: str, file_id: str) -> DriveFile:
        return _file(self._json(grant_id, f"/files/{check_id(file_id)}", {"fields": _FIELDS}))

    def children(self, grant_id: str, folder_id: str, *, page_token: str | None, page_size: int) -> Page:
        query = f"'{check_id(folder_id)}' in parents and trashed = false"  # the ID was checked: no quote can end the string
        params = {"q": query, "fields": f"nextPageToken,files({_FIELDS})", "pageSize": str(max(1, min(page_size, 1000))), "orderBy": "name"}
        if page_token is not None:
            if not isinstance(page_token, str) or len(page_token) > 1024:
                raise DriveUnavailable("the page token is unreadable")
            params["pageToken"] = page_token
        data = self._json(grant_id, "/files", params)
        files = data.get("files")
        if not isinstance(files, list):
            raise DriveUnavailable("the file list is unreadable")
        token = data.get("nextPageToken")
        return Page(tuple(_file(item) for item in files), token if isinstance(token, str) and token else None)

    def export_text(self, grant_id: str, file_id: str, *, max_bytes: int) -> bytes:
        return self._bytes(grant_id, f"/files/{check_id(file_id)}/export", {"mimeType": "text/plain"}, max_bytes)

    def download(self, grant_id: str, file_id: str, *, max_bytes: int) -> bytes:
        return self._bytes(grant_id, f"/files/{check_id(file_id)}", {"alt": "media"}, max_bytes)

    def _json(self, grant_id: str, path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            data = json.loads(self._bytes(grant_id, path, params, self._max_json))
        except ValueError as exc:
            raise DriveUnavailable("Google Drive's answer is not JSON") from exc
        if not isinstance(data, dict):
            raise DriveUnavailable("Google Drive's answer is not an object")
        return data

    def _bytes(self, grant_id: str, path: str, params: dict[str, str], max_bytes: int) -> bytes:
        url = f"{self.API}{path}?{urlencode(params)}"
        for attempt in range(self._retries + 1):
            try:
                status, body, reason = self._fetch(grant_id, url, max_bytes)
            except httpx.TransportError as exc:
                status, body, reason = 0, b"", type(exc).__name__
            if status == 200:
                return body
            if status == 401:
                raise DriveAuthorizationRequired("Google Drive refused the authorization (401): it was revoked or expired")
            retry = status in _RETRIED or status == 0 or (status == 403 and reason in _RATE_LIMITED)
            if not retry:
                raise DriveUnavailable(f"Google Drive answered {status}{f' ({reason})' if reason else ''}")
            if attempt < self._retries:
                self._sleep(self._backoff * 2**attempt)
        raise DriveUnavailable(f"Google Drive did not answer after {self._retries + 1} tries (last: {status or reason})")

    def _fetch(self, grant_id: str, url: str, max_bytes: int) -> tuple[int, bytes, str]:
        for _hop in range(_MAX_REDIRECTS + 1):
            decision = self._egress.check("google_drive", url)
            if not decision.allowed:
                raise DriveAccessRefused(f"egress policy refused {httpx.URL(url).host}: {decision.reason}")
            headers = {"Authorization": f"Bearer {self._tokens.token(grant_id)}"}
            with self._client.stream("GET", url, headers=headers, follow_redirects=False) as answer:
                if answer.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, answer.headers.get("location", ""))
                    continue
                body = b""
                for chunk in answer.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise DriveAccessRefused(f"the answer is over the {max_bytes}-byte limit")
                return answer.status_code, body, _reason(body) if answer.status_code != 200 else ""
        raise DriveUnavailable("too many redirects")


def _reason(body: bytes) -> str:
    """Google's own error reason, e.g. rateLimitExceeded. Never the message, which can echo request data."""
    try:
        errors = json.loads(body).get("error", {}).get("errors") or [{}]
        reason = errors[0].get("reason", "")
    except (ValueError, AttributeError, TypeError):
        return ""
    return reason if isinstance(reason, str) and reason.isascii() and len(reason) <= 60 else ""


def _file(item: Any) -> DriveFile:
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("mimeType"), str):
        raise DriveUnavailable("a file record is unreadable")
    size = item.get("size")
    parents = item.get("parents") or []
    return DriveFile(
        id=check_id(item["id"]), name=str(item.get("name", ""))[:300], mime_type=item["mimeType"], parents=tuple(p for p in parents if isinstance(p, str)),
        modified_time=str(item.get("modifiedTime", "")), size=int(size) if isinstance(size, str | int) and str(size).isdigit() else None, trashed=bool(item.get("trashed")),
        md5=item.get("md5Checksum") if isinstance(item.get("md5Checksum"), str) else None,
    )  # fmt: skip
