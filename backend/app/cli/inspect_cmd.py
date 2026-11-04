"""`devpilot inspect <GitHub URL>`: what a repository is and whether DevPilot could build from it. Free: GitHub only, no OpenAI."""

from __future__ import annotations

from app.cli import sources
from app.cli.build import IO, ConsoleIO, load_web_approval
from app.cli.paths import Paths
from app.cli.request import STACK_NAMES
from app.repos.github_url import RepoUrlError, parse_repo_url
from app.verification.recipes import readiness


def inspect_command(paths: Paths, url: str, *, ref: str | None = None, io: IO | None = None) -> int:
    io = io or ConsoleIO()
    try:
        repo = parse_repo_url(url, ref=ref)
    except RepoUrlError as exc:
        io.say(f"Refused: {exc}.")
        return 2
    found = sources.access(paths, repo, io.say)
    if found is None:
        return 2
    _gh, remote = found
    io.say(remote.describe())
    stack = remote.profile.stack
    if stack is None:
        io.say("\nDevPilot cannot build from this repository: " + (f"it uses {', '.join(remote.profile.unsupported)}." if remote.profile.unsupported else remote.profile.summary + "."))
        return 0
    load_web_approval(paths, io)
    ok, why = readiness(stack)
    io.say(f"\nStack: {stack} ({STACK_NAMES.get(stack, stack)})")
    io.say(("Builds from it are accepted: " if ok else "Builds from it are not accepted yet: ") + why)
    return 0
