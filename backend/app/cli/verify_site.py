"""`devpilot verify-site <folder or GitHub URL>`: check an existing website with the self-tested verifier. No OpenAI request.

The project is profiled from its own files; the component to check is the one static site or React + Vite app it holds
(or the one named with --component). The verifier works on a disposable copy, so the person's folder is never changed,
and the full report is saved as JSON beside a short summary. Frameworks the verifier does not cover are named and refused.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from app.cli import sources, webruntime
from app.cli.build import IO, ConsoleIO
from app.cli.paths import Paths
from app.repos.github_url import RepoUrlError, parse_repo_url
from app.repos.inspect import inspect_project
from app.verification import web


def _looks_like_url(target: str) -> bool:
    return bool(re.match(r"^(https?://|git@|github\.com/)", target.strip(), re.IGNORECASE))


def verify_site_command(paths: Paths, target: str, *, ref: str | None = None, component: str | None = None, output: Path | None = None, io: IO | None = None,
                        verifier: web.WebVerifier | None = None) -> int:  # fmt: skip
    io = io or ConsoleIO()
    paths.ensure()
    if _looks_like_url(target):
        try:
            repo = parse_repo_url(target if not target.lower().startswith("github.com/") else "https://" + target, ref=ref)
        except RepoUrlError as exc:
            io.say(f"Refused: {exc}.")
            return 2
        found = sources.access(paths, repo, io.say)
        if found is None:
            return 2
        gh, remote = found
        io.say(remote.describe())
        try:
            files = sources.fetch(gh, remote).files
        except Exception as exc:  # noqa: BLE001 - an access or import refusal, reported as it is
            io.say(f"The repository could not be downloaded: {exc}")
            return 1
        profile, name = remote.profile, remote.info.full_name.replace("/", "-")
    else:
        folder = Path(target).expanduser()
        if not folder.is_dir():
            io.say(f"Refused: {folder} is not a folder.")
            return 2
        try:
            files = web.read_folder(folder)
        except web.WebVerificationError as exc:
            io.say(f"Refused: {exc}.")
            return 2
        profile = inspect_project({p: len(d) for p, d in files.items()}, lambda p: files[p].decode("utf-8", "replace") if p in files and len(files[p]) <= 400_000 else None)
        name = folder.resolve().name
        io.say(profile.describe())

    candidates = [c for c in profile.components if c.kind in web.WEB_KINDS]
    if component is not None:
        candidates = [c for c in candidates if c.path == component.strip("/")]
    if not candidates:
        found_kinds = ", ".join(f"{c.path or '(root)'}: {c.framework}" for c in profile.components) or "nothing recognisable"
        io.say(f"\nNot verified: no static site or React + Vite app was found{' in ' + component if component else ''} ({found_kinds}).")
        return 2
    if len(candidates) > 1:
        io.say(f"\nSeveral sites were found ({', '.join(c.path or '(root)' for c in candidates)}): choose one with --component.")
        return 2
    chosen = candidates[0]

    verifier = verifier or web.WebVerifier(sources.egress(), work_dir=paths.work)
    image_id, why = webruntime.load_approval(paths, verifier)
    if image_id is None:
        io.say(f"\nNot verified: {why}.")
        return 1
    io.say(f"\nVerifying {chosen.path or 'the project'} as {chosen.framework} in Docker (dependencies through the npm-only proxy, then no network)…")
    try:
        result = verifier.verify(files, chosen.kind, component=chosen.path)
    except web.WebVerificationError as exc:
        io.say(f"Not verified: {exc}.")
        return 1
    io.say(result.describe())
    out = (output or Path.cwd() / "devpilot-output") / f"verify-{name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    io.say(f"Report: {out}")
    return 0 if result.passed else 1
