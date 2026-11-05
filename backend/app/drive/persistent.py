"""Google Drive consent that survives a backend restart (SECURITY.md §7): grants and encrypted refresh tokens on disk.

- `FileCredentialStore` keeps each grant's refresh token as the approved cipher's **ciphertext** (Fernet, DEVPILOT_TOKEN_KEY),
  one file per grant under CREDENTIALS_DIR/drive. It refuses anything that is not ciphertext bytes, exactly as the in-memory
  store does; a plaintext token never reaches the disk. Access tokens stay in memory and are refreshed after a restart.
- `FileGrantStore` keeps the grants themselves: which project, which folders or files, the read-only scope, the status and the
  dates. No credential is in it. A revoked grant stays recorded as revoked.
Every write goes to a temporary file first and then replaces the old one, so a crash never leaves half a record. Without
DRIVE_SOURCE=google nothing here is used.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

from app.drive.access import Grant, GrantStatus, GrantStore
from app.drive.oauth import CredentialStore, StoredCredential

_GRANT_ID = re.compile(r"^grant-[0-9a-f]{16}$")


def _write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class FileCredentialStore(CredentialStore):
    def __init__(self, directory: Path) -> None:
        super().__init__()
        self._dir = directory
        for file in sorted(directory.glob("grant-*.json")) if directory.is_dir() else []:
            try:
                raw = json.loads(file.read_text(encoding="utf-8"))
                credential = StoredCredential(str(raw["project_id"]), base64.b64decode(raw["ciphertext"]), frozenset(raw["scopes"]), float(raw["stored_at"]))
            except (OSError, ValueError, KeyError, TypeError):
                continue  # an unreadable record is not trusted; that grant must be authorized again
            if _GRANT_ID.fullmatch(file.stem) and credential.ciphertext:
                self._items[file.stem] = credential

    def _path(self, grant_id: str) -> Path:
        if not _GRANT_ID.fullmatch(grant_id):
            raise ValueError(f"invalid grant ID {grant_id!r}")
        return self._dir / f"{grant_id}.json"

    def put(self, grant_id: str, credential: StoredCredential) -> None:
        path = self._path(grant_id)
        super().put(grant_id, credential)  # refuses anything but ciphertext, before anything is written
        record = {"project_id": credential.project_id, "ciphertext": base64.b64encode(credential.ciphertext).decode("ascii"), "scopes": sorted(credential.scopes),
                  "stored_at": credential.stored_at}  # fmt: skip
        _write_atomically(path, json.dumps(record).encode("utf-8"))

    def delete(self, grant_id: str) -> bool:
        removed = super().delete(grant_id)
        try:
            self._path(grant_id).unlink()
            removed = True
        except (FileNotFoundError, ValueError):
            pass
        return removed


def _grant_to_json(grant: Grant) -> dict[str, Any]:
    return {"id": grant.id, "project_id": grant.project_id, "folders": list(grant.folders), "scopes": sorted(grant.scopes), "status": grant.status.value,
            "simulated": grant.simulated, "account": grant.account, "created_at": grant.created_at, "revoked_at": grant.revoked_at}  # fmt: skip


def _grant_from_json(raw: dict[str, Any]) -> Grant:
    return Grant(str(raw["id"]), str(raw["project_id"]), tuple(raw["folders"]), frozenset(raw["scopes"]), GrantStatus(raw["status"]), bool(raw["simulated"]),
                 str(raw["account"]), str(raw["created_at"]), raw.get("revoked_at"))  # fmt: skip


class FileGrantStore(GrantStore):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        if path.is_file():
            try:
                for raw in json.loads(path.read_text(encoding="utf-8")):
                    grant = _grant_from_json(raw)
                    if _GRANT_ID.fullmatch(grant.id):
                        self._grants[grant.id] = grant
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{path} is not a readable grant record; move it aside to start again") from exc

    def _save(self) -> None:
        _write_atomically(self._path, json.dumps([_grant_to_json(g) for g in self._grants.values()], indent=1).encode("utf-8"))

    def add(self, grant: Grant) -> Grant:
        added = super().add(grant)
        self._save()
        return added

    def revoke(self, grant_id: str, project_id: str) -> Grant:
        revoked = super().revoke(grant_id, project_id)
        self._save()
        return revoked

    def activate(self, grant_id: str, project_id: str) -> Grant:
        active = super().activate(grant_id, project_id)
        self._save()
        return active
