"""`publish_repository_changes`: put the verified project on GitHub, as a pull request or a new repository. GIT_PUSH: a person
approves every call, after seeing the repository, the branch and every file (SECURITY.md §10).

Code governs what can be published, whatever GPT asks:
- only after a passed verification, and only the tree that passed: the disposable copy's digest must equal the verified one;
- a pull request carries only the files the run added or changed against the copy it started from (an imported repository),
  on a new branch; deletions are listed but never pushed, and the default branch is never written;
- a new repository gets the whole verified project, with the imported licence and notices when code was reused;
- the same packaging rules as the ZIP: excluded names stay out, and a source file holding a credential stops the publish.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from functools import partial
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from app.coding.candidate import CandidateWorkspaces
from app.core.egress import EgressPolicy
from app.core.redaction import Redactor
from app.db.models import Project, Run, RunState
from app.repos.github_oauth import GitHubConnection
from app.repos.github_write import GitHubWriteError, GitHubWriter, Published, check_repo_name, describe_files
from app.repos.github_url import check_ref
from app.tools.adapters.repositories import imported_record
from app.tools.base import ErrorClass, ExternalStep, Progress, RiskLevel, ToolArgs, ToolContext, ToolError, ToolSpec
from app.tools.registry import ToolRegistry
from app.verification.artifact import omitted_source_reason, select

FullName = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")]
RepoName = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9._-]{1,100}$")]
Branch = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Body = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
_SLUG = re.compile(r"[^a-z0-9]+")


class PublishArgs(ToolArgs):
    mode: Literal["pull_request", "new_repository"] = Field(description="pull_request: the run's changes to an existing repository; new_repository: the whole project")
    repository: FullName | None = Field(description="owner/name of the repository for a pull request; null for a new repository")
    new_repository_name: RepoName | None = Field(description="The new repository's name; null for a pull request")
    private: bool | None = Field(description="New repository only: true for private (the safe default); null for a pull request")
    branch: Branch | None = Field(description="Pull request only: the new branch to create; null for devpilot/<project>")
    title: Title = Field(description="The pull request title, or the new repository's description")
    body: Body = Field(description="What changed and how it was verified")

    @model_validator(mode="after")
    def _one_target(self) -> PublishArgs:
        if self.mode == "pull_request" and (self.repository is None or self.new_repository_name is not None):
            raise ValueError("a pull request names repository and no new_repository_name")
        if self.mode == "new_repository" and (self.new_repository_name is None or self.repository is not None):
            raise ValueError("a new repository names new_repository_name and no repository")
        return self


@dataclass
class PublishRuntime:
    candidates: CandidateWorkspaces
    connection: GitHubConnection
    egress: EgressPolicy
    redactor: Redactor
    client: Any = None  # an httpx.Client for tests; None uses the writer's own


@dataclass(frozen=True)
class _Plan:
    runtime: PublishRuntime
    args: PublishArgs
    token: str
    files: dict[str, bytes]
    branch: str | None
    message: str


def _default_branch(project: Project, run: Run) -> str:
    return f"devpilot/{_SLUG.sub('-', project.name.lower()).strip('-')[:40] or 'project'}-{run.id[:6]}"


def _files(runtime: PublishRuntime, run_id: str, mode: str) -> tuple[dict[str, bytes], list[str], list[str]]:
    """The files to publish, their exclusions, and the deletions that are not pushed. Refuses as the ZIP would."""
    root = runtime.candidates.path(run_id)
    deleted: list[str] = []
    if mode == "pull_request":
        changes = runtime.candidates.changes(run_id)
        paths = [c.path for c in changes if c.kind in ("added", "modified")]
        deleted = [c.path for c in changes if c.kind == "deleted"]
    else:
        paths = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    selection, data = select(paths, lambda rel: (root / rel).read_bytes(), runtime.redactor)
    reason = omitted_source_reason(selection)
    if reason is not None:
        raise ToolError(ErrorClass.POLICY_REFUSED, f"nothing was published: {reason}")
    if not data:
        raise ToolError(ErrorClass.POLICY_REFUSED, "nothing was published: the run changed no file that may be published")
    return data, [f"{item.path}: {item.reason}" for item in selection.excluded], deleted


def _verified(runtime: PublishRuntime, ctx: ToolContext) -> None:
    last = json.loads(ctx.run.verification_json or "null")
    if not isinstance(last, dict) or last.get("verdict") != "PASSED":
        raise ToolError(ErrorClass.POLICY_REFUSED, "nothing is published without a passed verification")
    if not runtime.candidates.exists(ctx.run.id) or runtime.candidates.digest(ctx.run.id) != last.get("fingerprint"):
        raise ToolError(ErrorClass.POLICY_REFUSED, "the project changed after it was verified; only the verified files can be published")


def _begin(runtime: PublishRuntime, ctx: ToolContext, args: PublishArgs) -> _Plan:
    _verified(runtime, ctx)
    token = runtime.connection.token()
    if token is None:
        raise ToolError(ErrorClass.POLICY_REFUSED, "GitHub is not connected: the person must connect GitHub in the dashboard first", ["authorization_required"])
    files, excluded, deleted = _files(runtime, ctx.run.id, args.mode)
    branch = None
    if args.mode == "pull_request":
        branch = args.branch or _default_branch(ctx.project, ctx.run)
        try:
            check_ref(branch)
        except ValueError as exc:
            raise ToolError(ErrorClass.VALIDATION_FAILED, f"invalid branch name: {exc}") from exc
    else:
        imported = imported_record(ctx.session, ctx.run.id)
        if imported is not None:
            needed = [rel for rel in (imported["notices_file"], *imported["license_files"]) if rel not in files]
            if needed:
                raise ToolError(ErrorClass.POLICY_REFUSED, "reused code is published only with its licence and notices", needed)
        try:
            check_repo_name(str(args.new_repository_name))
        except GitHubWriteError as exc:
            raise ToolError(ErrorClass.VALIDATION_FAILED, str(exc)) from exc
    ctx.events.record(ctx.session, project_id=ctx.project.id, run_id=ctx.run.id, type="github.publish_started",
                      payload={"mode": args.mode, "repository": args.repository or args.new_repository_name, "branch": branch, "files": len(files),
                               "excluded": excluded[:20], "deletions_not_pushed": deleted[:50]})  # fmt: skip
    return _Plan(runtime, args, token, files, branch, f"{args.title}\n\nVerified by DevPilot AI (run {ctx.run.id}).")


def _run(plan: _Plan, _cancel: threading.Event | None, _progress: Progress) -> Published:
    writer = GitHubWriter(plan.runtime.egress, plan.token, client=plan.runtime.client)
    try:
        if plan.args.mode == "pull_request":
            assert plan.args.repository is not None and plan.branch is not None
            return writer.open_pull_request(plan.args.repository, files=plan.files, branch=plan.branch, base=None, title=plan.args.title, body=plan.args.body, message=plan.message)
        return writer.create_repository(str(plan.args.new_repository_name), private=plan.args.private is not False, description=plan.args.title, files=plan.files, message=plan.message)
    except GitHubWriteError as exc:
        error = ErrorClass.POLICY_REFUSED if exc.kind in ("unauthorized", "forbidden", "conflict", "invalid") else ErrorClass.TOOL_ERROR
        raise ToolError(error, f"GitHub did not accept the publish: {exc}", [exc.kind]) from exc


def _finish(ctx: ToolContext, plan: _Plan, published: Published) -> dict[str, object]:
    payload = published.to_dict() | {"mode": plan.args.mode}
    ctx.events.record(ctx.session, project_id=ctx.project.id, run_id=ctx.run.id, type="github.published", payload=payload)
    return {**payload, "note": "Published after the person's approval. Call finish to deliver the project here too."}


def _details(runtime: PublishRuntime, session: Any, run: Run, project: Project, args: PublishArgs) -> dict[str, Any]:
    """What the person sees before approving. It reads only the local copy; nothing is fetched."""
    status = runtime.connection.status()
    details: dict[str, Any] = {"mode": args.mode, "github_account": status.get("login"), "connected": status.get("connected"),
                               "target": args.repository if args.mode == "pull_request" else f"new {'public' if args.private is False else 'private'} repository {args.new_repository_name}",
                               "branch": (args.branch or _default_branch(project, run)) if args.mode == "pull_request" else "the new repository's default branch",
                               "title": args.title}  # fmt: skip
    try:
        if not runtime.candidates.exists(run.id):
            raise ToolError(ErrorClass.POLICY_REFUSED, "there is no verified copy to publish")
        files, excluded, deleted = _files(runtime, run.id, args.mode)
        details |= {"files": describe_files(files), "excluded": excluded, "deletions_not_pushed": deleted}
    except ToolError as exc:
        details["problem"] = exc.summary
    return details


def register_publish_tool(registry: ToolRegistry, runtime: PublishRuntime) -> None:
    registry.register(
        ToolSpec(
            name="publish_repository_changes",
            description="After a passed verification, publish the verified project to GitHub: the run's changes as a pull request on a new branch "
            "of an existing repository, or the whole project as a new repository. Needs the person's GitHub connection and approval.",
            args_model=PublishArgs,
            risk=RiskLevel.GIT_PUSH,
            states=frozenset({RunState.DOCUMENTING}),
            handler=None,
            external=ExternalStep(begin=partial(_begin, runtime), run=_run, finish=_finish),
            approval_details=partial(_details, runtime),
        )
    )
