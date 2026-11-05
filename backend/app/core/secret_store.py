"""Credentials the backend keeps between restarts, encrypted at rest (SECURITY.md §7 and §10).

One file per credential under `CREDENTIALS_DIR` (data/credentials by default), each the approved cipher's ciphertext of a
small JSON object. The approved cipher is Fernet keyed by DEVPILOT_TOKEN_KEY; without it nothing is stored (fail closed),
and no weaker substitute is used. Plain values never touch the disk, the logs or the database, and are kept in memory only
by the caller that needs them.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Protocol

_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")


class SecretStoreUnavailable(Exception):
    """Nothing can be stored or read: no approved cipher, or a stored value no longer decrypts."""


class Cipher(Protocol):
    def encrypt(self, plaintext: bytes) -> bytes: ...
    def decrypt(self, ciphertext: bytes) -> bytes: ...


class EncryptedFileStore:
    def __init__(self, directory: Path, cipher: Cipher | None, *, problem: str | None = None) -> None:
        self._dir = directory
        self._cipher = cipher
        self.problem = problem or ("no approved cipher is configured" if cipher is None else None)

    @property
    def available(self) -> bool:
        return self._cipher is not None

    def _path(self, name: str) -> Path:
        if not _NAME.fullmatch(name):
            raise ValueError(f"invalid credential name {name!r}")
        return self._dir / f"{name}.enc"

    def put(self, name: str, value: dict[str, Any]) -> None:
        if self._cipher is None:
            raise SecretStoreUnavailable(self.problem or "no approved cipher is configured")
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(self._cipher.encrypt(json.dumps(value, separators=(",", ":")).encode("utf-8")))
        os.replace(tmp, path)  # never a half-written credential

    def get(self, name: str) -> dict[str, Any] | None:
        path = self._path(name)
        if not path.is_file():
            return None
        if self._cipher is None:
            raise SecretStoreUnavailable(self.problem or "no approved cipher is configured")
        try:
            data = json.loads(self._cipher.decrypt(path.read_bytes()).decode("utf-8"))
        except SecretStoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - a wrong key or a damaged file: the credential is unusable, never guessed
            raise SecretStoreUnavailable("a stored credential cannot be decrypted with the current DEVPILOT_TOKEN_KEY") from exc
        return data if isinstance(data, dict) else None

    def delete(self, name: str) -> bool:
        path = self._path(name)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True
