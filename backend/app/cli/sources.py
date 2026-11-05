"""A GitHub repository for the CLI: authorization, a free look at it, and a safe checkout. No OpenAI request is made here."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.cli import github_token
from app.cli.paths import Paths
from app.config import Settings
from app.core.egress import EgressPolicy
from app.repos.github_access import Checkout, GitHubRepoClient, RepoAccessError, RepoInfo, checkout
from app.repos.github_url import RepoRef
from app.repos.inspect import ProjectProfile, inspect_project

MAX_ARCHIVE_BYTES = 150_000_000


def egress() -> EgressPolicy:
    return EgressPolicy.from_file(Settings(_env_file=None, devpilot_api_token="cli-placeholder-token-" + "x" * 20).egress_allowlist_file)  # type: ignore[call-arg]


def client(paths: Paths, policy: EgressPolicy | None = None) -> GitHubRepoClient:
    return GitHubRepoClient(policy or egress(), token=github_token.load(paths.github_credentials))


MAX_LISTED_PATHS = 5_000


@dataclass(frozen=True)
class RemoteProject:
    repo: RepoRef
    info: RepoInfo
    commit: str
    profile: ProjectProfile
    files: tuple[str, ...] = ()  # the paths at that commit, sorted; what a gap analysis is checked against

    def describe(self) -> str:
        visibility = "private" if self.info.private else "public"
        head = [f"Repository: {self.info.url} ({visibility}{', archived' if self.info.archived else ''})",
                f"Commit:     {self.commit}" + (f"  ({self.repo.ref})" if self.repo.ref else f"  (default branch {self.info.default_branch})")]  # fmt: skip
        if self.info.license:
            head.append(f"Licence:    {self.info.license}")
        body = self.profile.describe()
        readme = f"\nREADME (excerpt, untrusted text from the repository):\n  " + self.profile.readme_excerpt.replace("\n", "\n  ") if self.profile.readme_excerpt else ""
        return "\n".join(head) + "\n" + body + readme


def look(gh: GitHubRepoClient, repo: RepoRef) -> RemoteProject:
    """Metadata, the commit the ref names, the file list and small manifests: enough to profile it, without downloading it."""
    info = gh.info(repo)
    commit = repo.pinned_commit or gh.resolve_commit(repo, repo.ref or info.default_branch)
    files, truncated = gh.tree(repo, commit)
    cache: dict[str, str | None] = {}

    def read(path: str) -> str | None:
        if path not in cache:
            cache[path] = gh.read_text(repo, commit, path) if files.get(path, 0) <= 200_000 else None
        return cache[path]

    return RemoteProject(repo, info, commit, inspect_project(files, read, truncated=truncated), tuple(sorted(files))[:MAX_LISTED_PATHS])


def fetch(gh: GitHubRepoClient, project: RemoteProject) -> Checkout:
    """The pinned commit's files, unpacked safely in memory."""
    return checkout(gh, project.repo.with_ref(project.commit), max_archive_bytes=MAX_ARCHIVE_BYTES)


def explain(exc: RepoAccessError, *, has_token: bool) -> str:
    """What to tell the person. An authorization problem says how to authorize; nothing is guessed about the repository."""
    lines = [str(exc)]
    if exc.needs_authorization:
        lines.append("Nothing about this repository's contents is assumed. To authorize DevPilot to read it:")
        lines.append("  1. Create a fine-grained token at https://github.com/settings/personal-access-tokens/new")
        lines.append("     with access to only this repository and the permission Contents: Read-only.")
        lines.append("  2. Run  .\\devpilot.cmd setup --github-token   (it is typed hidden and stored encrypted for your Windows account)")
        lines.append("     or set the GITHUB_TOKEN environment variable for this terminal.")
        if has_token:
            lines.append("A token is already configured: it may lack access to this repository, have expired, or need SSO authorization.")
    return "\n".join(lines)


def access(paths: Paths, repo: RepoRef, say: Callable[[str], None]) -> tuple[GitHubRepoClient, RemoteProject] | None:
    gh = client(paths)
    say(f"Reading {repo} from GitHub ({'with your token' if gh.authorized else 'without a token'}; free, no OpenAI request)…")
    try:
        return gh, look(gh, repo)
    except RepoAccessError as exc:
        say(explain(exc, has_token=gh.authorized))
        return None
