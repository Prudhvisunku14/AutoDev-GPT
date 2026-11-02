"""What DevPilot knows about a public repository.

Every text field that came from a repository (description, README, manifests, license text) is untrusted data. It
is evaluated by Python and never followed as an instruction, and the README never reaches GPT.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FIXTURE, GITHUB = "fixture", "github"  # where repository data came from; a fixture is never presented as GitHub
FULL_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class RepoSummary:
    full_name: str
    url: str
    description: str
    language: str | None
    stars: int
    license: str | None  # the SPDX id the host reports, or None
    archived: bool
    pushed_at: str | None  # ISO date of the last push
    default_branch: str
    size_kb: int
    topics: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepoDetail:
    summary: RepoSummary
    commit: str  # the default branch's head when it was read: what an import is pinned to
    files: dict[str, int]  # path -> bytes, from the repository tree
    readme: str  # untrusted
    manifests: dict[str, str]  # package.json, requirements*.txt, pyproject.toml: untrusted, read only for evidence
    license_text: str  # untrusted; kept for attribution
    source: str  # FIXTURE or GITHUB
    truncated: bool = False  # the host's tree listing was cut short
    notes: list[str] = field(default_factory=list)
