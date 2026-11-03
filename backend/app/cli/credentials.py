"""The user's own OpenAI key. It is never printed, logged, committed, sent anywhere but OpenAI, or kept as plain text.

Sources, in order: the OPENAI_API_KEY environment variable, then a file encrypted with Windows DPAPI, which only the same Windows
user on the same machine can decrypt. The repository's `.env` is never read by the CLI. Off Windows there is no encrypted store, and
the environment variable is the only source.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes import wintypes
from pathlib import Path

ENV_VAR = "OPENAI_API_KEY"
_PLAUSIBLE = re.compile(r"^sk-[A-Za-z0-9_\-]{20,}$")
_DESCRIPTION = "DevPilot OpenAI key"


class CredentialError(Exception):
    pass


def plausible(key: str) -> bool:
    """Whether this looks like an OpenAI key. A format check only: it says nothing about whether OpenAI accepts it."""
    return bool(_PLAUSIBLE.fullmatch(key))


class _Blob(ctypes.Structure):
    _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char)))


def _crypt32() -> ctypes.WinDLL:  # type: ignore[name-defined]
    lib = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
    for name in ("CryptProtectData", "CryptUnprotectData"):
        function = getattr(lib, name)
        function.restype = wintypes.BOOL
    lib.CryptProtectData.argtypes = (ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob))
    lib.CryptUnprotectData.argtypes = (ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob))
    return lib


def _transform(data: bytes, *, protect: bool) -> bytes:
    lib = _crypt32()
    source = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    result = _Blob()
    forbid_ui = 0x1
    ok = lib.CryptProtectData(ctypes.byref(source), _DESCRIPTION, None, None, None, forbid_ui, ctypes.byref(result)) if protect else lib.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, forbid_ui, ctypes.byref(result)
    )
    if not ok:
        raise CredentialError("Windows could not protect or unprotect the stored key (it may belong to another Windows user)")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        kernel.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def store_available() -> bool:
    return sys.platform == "win32"


def store_key(path: Path, key: str) -> None:
    if not store_available():
        raise CredentialError(f"There is no encrypted store on this system: set the {ENV_VAR} environment variable instead")
    if not plausible(key):
        raise CredentialError("That does not look like an OpenAI API key (it starts with sk-)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_transform(key.encode("utf-8"), protect=True))


def stored_key(path: Path) -> str | None:
    if not store_available() or not path.is_file():
        return None
    try:
        return _transform(path.read_bytes(), protect=False).decode("utf-8")
    except (OSError, CredentialError, UnicodeDecodeError):
        return None


def load_key(path: Path) -> str | None:
    """The key to use, or None. The environment variable wins, so a CI run or a one-off key needs no stored file."""
    from_env = os.environ.get(ENV_VAR, "").strip()
    return from_env or stored_key(path)


def source(path: Path) -> str | None:
    """Where a key would come from, without reading its value into any output."""
    if os.environ.get(ENV_VAR, "").strip():
        return f"the {ENV_VAR} environment variable"
    return "the encrypted store" if stored_key(path) else None


def forget(path: Path) -> bool:
    if path.is_file():
        path.unlink()
        return True
    return False
