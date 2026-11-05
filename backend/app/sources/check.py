"""Whether DevPilot can read each resource a request names, and what it found there. Run when the person creates a project.

- **GitHub:** a repository the person named is read automatically, at a pinned commit: metadata, the file list and small
  manifests, enough to profile it (`cli.sources.look`). Nothing is downloaded or run. It uses the person's GitHub connection
  when there is one. A refusal is reported by its kind: a 404 without a connection may mean private *or* a wrong name, so the
  answer asks for a connection and says both, never that the repository is private.
- **Google Drive:** a file or folder is readable only under an active grant of this project that covers it. Otherwise the
  answer asks for Google permission; nothing is read before it.

Reading needs the integration enabled (REPOSITORY_SOURCE=github, DRIVE_SOURCE=google); a disabled one is reported as such.
"""

from __future__ import annotations

from typing import Any

from app.drive.access import GrantStatus
from app.repos.github_access import GitHubRepoClient, RepoAccessError
from app.sources.discover import Discovery, DriveResource, GitHubResource

_GITHUB_MESSAGES = {
    "authorization_required": "GitHub did not show {name} without a connection. It may be private, or the name may be wrong. Connect GitHub to let DevPilot read repositories your account can see.",
    "token_rejected": "GitHub rejected the saved connection (it expired or was revoked). Connect GitHub again.",
    "not_visible": "{name} is not visible to your GitHub account: check the name, or ask its owner for access.",
    "forbidden": "GitHub refused access to {name} for your account (it may need single sign-on authorization for its organization).",
    "rate_limited": "GitHub's rate limit was reached. Try again later; a GitHub connection raises the limit.",
    "ref_not_found": "{name} has no branch, tag or commit with that name.",
    "too_large": "{name} is larger than DevPilot reads.",
}
NEEDS_CONNECTION = frozenset({"authorization_required", "token_rejected", "rate_limited"})


def _github(resource: GitHubResource, client: GitHubRepoClient | None, connected: bool, connect_blocker: str | None) -> dict[str, Any]:
    from app.cli.sources import look  # the same reading `devpilot inspect` does

    base = resource.to_dict()
    if client is None:
        return base | {"status": "disabled", "message": "Reading GitHub is off in this backend (REPOSITORY_SOURCE is not github).", "action": None}
    try:
        found = look(client, resource.repo)
    except RepoAccessError as exc:
        message = _GITHUB_MESSAGES.get(exc.kind, str(exc)).format(name=resource.repo.full_name)
        action = "connect_github" if (exc.kind in NEEDS_CONNECTION or (exc.kind == "forbidden" and not connected)) else None
        if action and connect_blocker:
            message += f" (Connecting GitHub is not set up in this backend: {connect_blocker}.)"
            action = None
        return base | {"status": exc.kind, "message": message, "action": action, "connected": connected}
    profile = found.profile
    return base | {
        "status": "accessible", "action": None, "connected": connected, "message": f"Read {found.info.full_name} at {found.commit[:12]}.",
        "visibility": "private" if found.info.private else "public", "commit": found.commit, "default_branch": found.info.default_branch,
        "archived": found.info.archived, "license": found.info.license, "stack": profile.stack, "summary": profile.summary,
        "components": [{"path": c.path or "(root)", "kind": c.kind, "framework": c.framework} for c in profile.components],
        "unsupported": list(profile.unsupported), "file_count": profile.file_count, "describe": found.describe()[:4_000],
    }  # fmt: skip


def _drive(resource: DriveResource, runtime: Any, project_id: str) -> dict[str, Any]:
    base = resource.to_dict()
    if runtime is None:
        return base | {"status": "disabled", "message": "Google Drive is off in this backend (DRIVE_SOURCE is not google).", "action": None}
    active = [g for g in runtime.grants.for_project(project_id) if g.status is GrantStatus.GRANTED and resource.drive_id in g.folders]
    if active:
        return base | {"status": "accessible", "message": "Covered by this project's Google Drive authorization.", "action": None, "grant_id": active[0].id}
    blocker = getattr(runtime.authorizer, "blocker", None)
    why = blocker() if callable(blocker) else None
    if why:
        return base | {"status": "authorization_required", "message": f"Google permission is needed, but consent is not set up in this backend: {why}.", "action": None}
    return base | {"status": "authorization_required", "message": "DevPilot needs your permission to read this from Google Drive (read-only, only what you choose).", "action": "connect_google"}


def check(discovery: Discovery, *, github: GitHubRepoClient | None, connected: bool, connect_blocker: str | None, drive: Any, project_id: str) -> list[dict[str, Any]]:
    results = []
    for resource in discovery.resources:
        if isinstance(resource, GitHubResource):
            results.append(_github(resource, github, connected, connect_blocker))
        else:
            results.append(_drive(resource, drive, project_id))
    return results


def for_gpt(results: list[dict[str, Any]]) -> str:
    """The resources block of GPT's first message: what was found, as untrusted data."""
    lines = []
    for item in results:
        if item["kind"] == "github" and item["status"] == "accessible":
            lines.append(f"GitHub repository {item['full_name']} ({item['visibility']}, commit {item['commit'][:12]}, licence {item.get('license') or 'none found'}):\n{item['describe']}")
        else:
            lines.append(f"{item['label']}: {item['status']}. {item['message']}")
    return "\n\n".join(lines)
