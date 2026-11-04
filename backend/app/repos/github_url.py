"""A GitHub repository named by a person: parsed, validated and normalised before anything is fetched.

Only `https://github.com/<owner>/<repo>` (with an optional `.git`, `/tree/<ref>` or `/commit/<sha>`) and the SSH form
`git@github.com:<owner>/<repo>.git` are accepted. Anything else is refused with the reason, before any request:
another scheme or host (including look-alikes such as `github.com.example.org`), a port, credentials embedded in the
URL, a query or fragment, percent-encoding, `..`, or a ref that git itself would reject. A refused URL is never echoed
back in full when it carried credentials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.repos.model import COMMIT, FULL_NAME

_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_NAME = r"[A-Za-z0-9._-]{1,100}"
_SSH = re.compile(rf"^git@github\.com:(?P<owner>{_OWNER})/(?P<name>{_NAME})$")
# Candidates in free text: anything that looks like it points at GitHub, so a malformed one is reported, not ignored.
_IN_TEXT = re.compile(r"(?:https?://|git@)[^\s<>\"'`]*github[^\s<>\"'`]*|(?<![\w/.-])github\.com/[^\s<>\"'`]+", re.IGNORECASE)
_OTHER_HOSTS = re.compile(r"https?://(?:www\.)?(gitlab\.com|bitbucket\.org|codeberg\.org|dev\.azure\.com|sourceforge\.net)/\S+", re.IGNORECASE)
_REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


class RepoUrlError(ValueError):
    """The text names a repository DevPilot will not fetch, and says why."""


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str
    ref: str | None = None  # a branch, tag or commit SHA; None means the default branch

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"

    @property
    def pinned_commit(self) -> str | None:
        return self.ref if self.ref and COMMIT.fullmatch(self.ref) else None

    def with_ref(self, ref: str | None) -> RepoRef:
        if ref is not None:
            check_ref(ref)
        return RepoRef(self.owner, self.name, ref)

    def __str__(self) -> str:
        return self.url + (f" @ {self.ref}" if self.ref else "")


def check_ref(ref: str) -> str:
    """A branch, tag or full SHA, by git's own rules for ref names (git check-ref-format), kept to plain characters."""
    bad = (
        not _REF.fullmatch(ref) or ref.startswith(("-", "/", ".")) or ref.endswith(("/", ".", ".lock")) or ".." in ref or "//" in ref or "/." in ref or "@{" in ref
    )
    if bad:
        raise RepoUrlError(f"{ref[:60]!r} is not a valid branch, tag or commit name")
    return ref


def _name(raw: str) -> str:
    name = raw[:-4] if raw.lower().endswith(".git") else raw
    if not name or name in (".", "..") or name.startswith("."):
        raise RepoUrlError("the repository name is missing or invalid")
    return name


def parse_repo_url(text: str, *, ref: str | None = None) -> RepoRef:
    """The repository a URL names. `ref` (from a --branch or --commit option) overrides one in the URL."""
    raw = text.strip()
    if any(ord(c) < 33 or ord(c) > 126 for c in raw):
        raise RepoUrlError("the URL has spaces, control or non-ASCII characters")
    ssh = _SSH.fullmatch(raw)
    if ssh:
        repo = RepoRef(ssh["owner"], _name(ssh["name"]))
    else:
        parts = urlsplit(raw)
        if parts.scheme.lower() != "https":
            raise RepoUrlError("only https://github.com/<owner>/<repository> URLs are accepted" + (" (http is not)" if parts.scheme.lower() == "http" else ""))
        if "@" in parts.netloc:
            raise RepoUrlError("the URL contains credentials; remove them and give a token through `devpilot setup --github-token` instead")
        if parts.port is not None or ":" in parts.netloc:
            raise RepoUrlError("the URL must not name a port")
        host = (parts.hostname or "").lower()
        if host not in ("github.com", "www.github.com"):
            raise RepoUrlError(f"only github.com repositories are supported, not {host or 'this address'}")
        if parts.query or parts.fragment:
            raise RepoUrlError("the URL must not have a query or fragment")
        if "%" in parts.path or "\\" in parts.path:
            raise RepoUrlError("the URL must not use percent-encoding or backslashes")
        segments = [s for s in parts.path.split("/") if s != ""]
        if any(s in (".", "..") for s in segments):
            raise RepoUrlError("the URL must not contain '.' or '..' segments")
        if len(segments) < 2:
            raise RepoUrlError("the URL must name both an owner and a repository: https://github.com/<owner>/<repository>")
        owner, name = segments[0], _name(segments[1])
        rest = segments[2:]
        url_ref = None
        if rest:
            if rest[0] in ("tree", "commit") and len(rest) >= 2:
                url_ref = "/".join(rest[1:])
            else:
                raise RepoUrlError("only the repository itself, /tree/<branch> or /commit/<sha> can be given")
        repo = RepoRef(owner, name, check_ref(url_ref) if url_ref else None)
    if not FULL_NAME.fullmatch(repo.full_name):
        raise RepoUrlError(f"{repo.full_name!r} is not a valid owner/repository name")
    return repo.with_ref(ref) if ref is not None else repo


def find_repo_url(prompt: str) -> RepoRef | None:
    """The one GitHub repository a request names, or None. Two different ones, or a malformed one, are refused."""
    other = _OTHER_HOSTS.search(prompt)
    candidates = [m.group(0).rstrip(".,;:)]}!?") for m in _IN_TEXT.finditer(prompt)]
    if not candidates:
        if other:
            raise RepoUrlError(f"only GitHub repositories are supported, not {other.group(1).lower()}")
        return None
    found = {parse_repo_url(c if c.lower().startswith(("https://", "http://", "git@")) else "https://" + c) for c in candidates}
    names = {r.full_name.lower() for r in found}
    if len(names) > 1:
        raise RepoUrlError("the request names more than one GitHub repository; give exactly one")
    return sorted(found, key=lambda r: r.ref or "")[-1]  # the one with a ref, when the same repository appears twice
