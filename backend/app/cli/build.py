"""`devpilot build`: ask what to build, get the person's typed spending approval, run the shared engine, save a verified ZIP.

The build is driven through the same API the dashboard uses (create project, create run, start it with the approved cap), so every
policy check, spending limit, approval gate, verification step and delivery gate is the engine's own. This module only asks, shows
progress, answers GPT's questions, and packages the result with `app.verification.artifact`, the code that also serves downloads.
"""

from __future__ import annotations

import getpass
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.cli import credentials, runtime, sources, webruntime
from app.cli.doctor import blockers, render, run_checks
from app.cli.engine import API, EngineRefused, LiveEngine, Session
from app.cli.paths import Paths
from app.cli.request import Decision, classify, decide
from app.cli.userconfig import MAX_CAP_USD, UserConfig
from app.repos.github_access import Checkout, RepoAccessError
from app.repos.github_url import RepoUrlError
from app.repos.importer import ImportRefused, import_files
from app.stacks.catalog import StackCatalog
from app.verification import artifact, web
from app.verification.recipes import readiness

MIN_PROMPT, MAX_PROMPT = 15, 19_000
DEFAULT_STACK = "python-cli"
POLL_SECONDS = 1.0
HOW_TO_RUN = "DEVPILOT-HOW-TO-RUN.txt"

class IO(Protocol):
    def ask(self, prompt: str) -> str: ...
    def say(self, text: str = "") -> None: ...


class ConsoleIO:
    def ask(self, prompt: str) -> str:
        return input(prompt)

    def say(self, text: str = "") -> None:
        print(text, flush=True)


def secret_prompt(prompt: str) -> str:
    return getpass.getpass(prompt)


def unsupported_request(prompt: str) -> str | None:
    """Why a request without a repository cannot be built now, or None. With a repository, its files decide (see build_command)."""
    try:
        request = classify(prompt)
    except RepoUrlError as exc:
        return f"Refused: {exc}."
    if request.repo is not None:
        return None
    decision = decide(request)
    return None if decision.accepted else f"Not built: {decision.reason}."


def validate_prompt(prompt: str) -> str | None:
    """Length only. What is asked for is decided by `classify` and `decide`, from the request and any repository's own files."""
    text = prompt.strip()
    if not text:
        return "Describe the application you want to build."
    if len(text) < MIN_PROMPT:
        return f"Add a little more detail: at least {MIN_PROMPT} characters."
    if len(text) > MAX_PROMPT:
        return f"That is {len(text):,} characters; the limit is {MAX_PROMPT:,}."
    return None


def project_name(prompt: str) -> str:
    words = " ".join(prompt.split())
    sentence = re.split(r"(?<=[.!?])\s", words)[0]
    return sentence.rstrip(".!?") if len(sentence) <= 60 else sentence[:59].rstrip() + "…"


def available_stacks() -> list[tuple[str, str, bool, str]]:
    """(id, name, buildable now, why not), from the same catalog and verification recipes the backend uses."""
    catalog = StackCatalog.load()
    result = []
    for stack_id in catalog.ids:
        entry = catalog.get(stack_id)
        ok, why = readiness(stack_id)
        result.append((stack_id, entry.name if entry else stack_id, ok, "" if ok else why))
    return result


def choose_stack(requested: str | None, io: IO, interactive: bool) -> str | None:
    stacks = available_stacks()
    ready = {s[0]: s for s in stacks if s[2]}
    if requested is not None:
        if requested in ready:
            return requested
        known = {s[0]: s for s in stacks}
        io.say(f"The stack {requested!r} cannot be built yet: " + (known[requested][3] if requested in known else f"it is not one of {', '.join(known)}"))
        return None
    for stack_id, name, ok, _why in stacks:
        io.say(f"  {stack_id:16} {name}" + ("" if ok else "  (not available yet: its verification runtime has not passed a real test)"))
    if len(ready) == 1:  # nothing to choose between: say what is used instead of asking
        only = next(iter(ready))
        io.say(f"Stack: {only}, the only one verified end to end so far.")
        return only
    default = DEFAULT_STACK if DEFAULT_STACK in ready else next(iter(ready), None)
    while interactive and default is not None:
        answer = io.ask(f"Stack [{default}]: ").strip() or default
        if answer in ready:
            return answer
        io.say(f"Choose one of: {', '.join(ready)}.")
    return default


# --- Spending approval ------------------------------------------------------------------------------------


def approval_text(config: UserConfig, cap: float) -> str:
    return (
        "\nSpending approval\n"
        f"  Model: {config.model}  (${config.price_input_per_mtok} in / ${config.price_output_per_mtok} out per 1M tokens: {config.prices_note})\n"
        f"  This build may spend at most ${cap:.2f}, GPT planning and OpenCode coding together.\n"
        "  Its real cost cannot be known beforehand: it depends on the plan and on how many repairs the tests need.\n"
        f"  What is certain is the limit: before every model request the most it could cost is worked out, and a request that could\n"
        f"  take the build past ${cap:.2f} is not sent. A build that hits the limit stops and is reported as stopped, not as a success.\n"
        "  Your OpenAI invoice is the authority on what was actually billed.\n"
    )


def confirm_spend(config: UserConfig, cap: float, io: IO, approve_flag: float | None) -> bool:
    """The person's own go-ahead. Only the typed phrase or an exact --approve-spend figure counts; nothing else starts a paid build."""
    io.say(approval_text(config, cap))
    if approve_flag is not None:
        if abs(approve_flag - cap) > 1e-9:
            io.say(f"Refused: --approve-spend {approve_flag:.2f} is not this build's limit of ${cap:.2f}.")
            return False
        io.say(f"Approved on the command line: --approve-spend {cap:.2f}.")
        return True
    phrase = f"approve {cap:.2f}"
    return io.ask(f'Type "{phrase}" to start the build, or anything else to cancel: ').strip().lower() == phrase


# --- Progress ---------------------------------------------------------------------------------------------

_STATE_WORDS = {
    "ANALYZING": "Analysing your request", "PLANNING": "Planning the work", "DEFINING_CRITERIA": "Defining how it will be tested", "IMPLEMENTING": "Writing the code",
    "VERIFYING": "Testing", "REPAIRING": "Repairing a failed test", "DOCUMENTING": "Preparing delivery",
}


def describe_event(event: dict[str, Any]) -> str | None:
    """One readable line for the events a person cares about, or None. Nothing is invented: each line comes from a recorded event."""
    p = event.get("payload") or {}
    kind = event.get("type")
    if kind == "run.state_changed":
        return _STATE_WORDS.get(str(p.get("to")))
    if kind == "analysis.accepted":
        return f"Requirements understood: {len(p.get('requirements') or [])}" if isinstance(p.get("requirements"), list) else "Requirements understood"
    if kind == "plan.accepted":
        return f"Plan accepted: {p.get('tasks') if not isinstance(p.get('tasks'), list) else len(p['tasks'])} task(s) on {p.get('stack', '?')}"
    if kind == "criteria.accepted":
        return "Acceptance criteria accepted"
    if kind == "acceptance.frozen":
        return f"Browser acceptance checks frozen before coding: {p.get('checks')} (the coding tool never sees them)"
    if kind == "repository.imported":
        return f"Imported {p.get('full_name')} at {str(p.get('commit', ''))[:12]}"
    if kind == "verification.website":
        failed = [s.get("name") for s in p.get("stages") or [] if isinstance(s, dict) and s.get("ok") is not True]
        return f"Browser run {p.get('verdict')}: {len(p.get('pages') or [])} page(s) in Chromium" + (f"; failed: {', '.join(map(str, failed))}" if failed else "")
    if kind == "coding.started":
        return f"Coding task {p.get('task', p.get('task_order', '?'))} started"
    if kind == "coding.finished":
        changes = p.get("changes") if isinstance(p.get("changes"), list) else []
        return f"Coding task {p.get('task_order', '?')}: {p.get('status')}, " + (f"{len(changes)} file(s) kept" if p.get("kept") else "undone") + (f" ({p['detail']})" if p.get("detail") and not p.get("kept") else "")
    if kind == "verification.started":
        return "Running the tests in the Docker sandbox"
    if kind == "verification.finished":
        return f"Tests {p.get('verdict')}: {p.get('passed')} of {p.get('ran')} passed" + (f" — {p['reason']}" if p.get("reason") and p.get("verdict") != "PASSED" else "")
    if kind == "acceptance.finished":
        return f"Independent acceptance {p.get('verdict')}: {p.get('passed')} of {p.get('total')}"
    if kind == "recovery.decided":
        return f"Recovery: {p.get('category') or p.get('failure_class')} → {p.get('action')}"
    if kind == "delivery.finished":
        files = p.get("files")
        return f"Delivered {len(files) if isinstance(files, list) else '?'} file(s)"
    if kind == "llm.error":
        return f"OpenAI error ({p.get('kind')}): {str(p.get('message', ''))[:160]}"
    if kind in ("run.failed", "run.stopped"):
        return f"The run stopped ({p.get('reason', 'error')}): {str(p.get('detail', ''))[:200]}"
    if kind == "run.cancelled":
        return "The run was cancelled"
    if kind == "run.completed":
        return "The run completed"
    return None


# --- Running a build ---------------------------------------------------------------------------------------


@dataclass
class BuildResult:
    state: str
    cost_usd: float | None
    zip_path: Path | None = None
    folder: Path | None = None
    reason: str | None = None  # why there is no ZIP
    verification: str | None = None

    @property
    def ok(self) -> bool:
        return self.zip_path is not None


def _json(response: Any, what: str) -> Any:
    if response.status_code >= 400:
        detail = ""
        try:
            detail = str(response.json().get("detail", ""))
        except (ValueError, AttributeError):
            detail = response.text[:200]
        raise BuildError(f"{what} was refused: {detail or response.status_code}")
    return response.json()


class BuildError(Exception):
    pass


def _decide(session: Session, approval: dict[str, Any], io: IO) -> bool:
    """The person decides in the terminal; nothing is approved for them. False: they stopped the run."""
    payload = approval["payload"] if isinstance(approval.get("payload"), dict) else {}
    if approval["kind"] == "HUMAN_INPUT":
        io.say(f"\nGPT asks: {payload.get('question', '')}\n  Why: {payload.get('reason', '')}")
        answer = io.ask("Your answer (just press Enter to stop the build): ").strip()
        if not answer:
            return False
        body: dict[str, Any] = {"decision": "approve", "response": answer}
    else:
        io.say(f"\nApproval needed for {approval['tool']} ({approval['risk']}):\n  {json.dumps(approval.get('details') or payload, ensure_ascii=False)[:1200]}")
        body = {"decision": "approve" if io.ask("Type yes to approve; anything else denies: ").strip().lower() == "yes" else "deny"}
    _json(session.client.post(f"{API}/approvals/{approval['id']}/decision", json=body, headers=session.headers), "the decision")
    return True


def _wait(session: Session, run_id: str, io: IO, *, sleep: Callable[[float], None], clock: Callable[[], float]) -> dict[str, Any]:
    deadline = clock() + session.settings.run_wall_clock_seconds + 120.0
    seq = 0
    while True:
        run = _json(session.client.get(f"{API}/runs/{run_id}", headers=session.headers), "reading the run")
        events = _json(session.client.get(f"{API}/runs/{run_id}/events", params={"after_seq": seq, "limit": 500}, headers=session.headers), "reading the events")
        for event in events:
            seq = max(seq, event["seq"])
            line = describe_event(event)
            if line:
                io.say(f"  • {line}")
        if run["ended_at"]:
            return run
        if run["pause_reason"]:
            io.say(f"The build paused ({run['pause_reason']}), so it is being closed: nothing carries on spending unattended.")
            return _cancel(session, run_id)
        if run["state"] == "AWAITING_APPROVAL":
            for approval in _json(session.client.get(f"{API}/approvals", params={"run_id": run_id}, headers=session.headers), "reading the questions"):
                if not _decide(session, approval, io):
                    io.say("Stopping the build at your request.")
                    return _cancel(session, run_id)
            deadline = clock() + session.settings.run_wall_clock_seconds + 120.0  # time spent answering is not the build's
        elif clock() > deadline:
            io.say("The build did not finish in time, so it is being closed.")
            return _cancel(session, run_id)
        sleep(POLL_SECONDS)


def _cancel(session: Session, run_id: str) -> dict[str, Any]:
    session.client.post(f"{API}/runs/{run_id}/cancel", headers=session.headers)
    return _json(session.client.get(f"{API}/runs/{run_id}", headers=session.headers), "reading the run")


def how_to_run(report: dict[str, Any], files: list[str]) -> str:
    project = (report.get("project") or {}).get("name") or "This project"
    if (report.get("project") or {}).get("stack") == "static-html":
        return _how_to_run_site(project, report, files)
    tests = [f for f in files if re.search(r"(^|/)test[^/]*\.py$", f)]
    mains = [f for f in files if f.endswith(".py") and f not in tests]
    lines = [
        f"{project}", "=" * len(project), "",
        "Generated and tested by DevPilot AI (see devpilot-verification/ for the report, and MANIFEST.txt for what is in this archive).", "",
        "Requirements: Python 3.12 (the version it was tested with). No other packages are needed unless a README here says so.", "",
        "Run the tests:", '    python -m unittest discover -v -s . -p "test*.py"', "",
    ]
    if mains:
        lines += ["Python files:", *[f"    {f}" for f in mains[:12]], "", "Open the README here (if there is one) for how to use it."]
    return "\n".join(lines) + "\n"


def _how_to_run_site(project: str, report: dict[str, Any], files: list[str]) -> str:
    """Instructions for a static website, from the delivered files and the report's own acceptance record."""
    pages = sorted(f for f in files if f.lower().endswith((".html", ".htm")))
    independent = report.get("acceptance") or {}
    proved = [c for c in independent.get("criteria", []) if c.get("status") == "PASSED"]
    lines = [
        f"{project}", "=" * len(project), "",
        "A static website (HTML, CSS and JavaScript) generated by DevPilot AI and verified in a real browser.",
        "See devpilot-verification/ for the full report, and MANIFEST.txt for what is in this archive.", "",
        "Open it:", "    Open index.html in a web browser, or serve the folder (some browsers restrict scripts on file:// pages):",
        "    python -m http.server 8000      then browse to http://localhost:8000/", "",
        "No build step and no packages are needed. It was verified with no network, so it does not depend on other sites.", "",
    ]
    if pages:
        lines += ["Pages:", *[f"    {p}" for p in pages[:20]], ""]
    if independent.get("kind") == "browser":
        lines += [f"Independent browser acceptance: {independent.get('verdict')} ({len(proved)} of {len(independent.get('criteria', []))} criteria passed), "
                  f"checks frozen before coding, SHA-256 {independent.get('suite_sha256')}.", ""]  # fmt: skip
    return "\n".join(lines) + "\n"


def package(session: Session, run: dict[str, Any], output: Path, io: IO) -> BuildResult:
    """The delivered files, as a folder and a ZIP, if and only if the engine's own rules say this is a verified delivery."""
    state = session.app.state
    report = _json(session.client.get(f"{API}/runs/{run['id']}/report", headers=session.headers), "reading the report")
    cost = report.get("run", {}).get("cost_usd")
    reason = artifact.unavailable_reason(report)
    if reason is not None:
        return BuildResult(run["state"], cost, reason=reason)
    fs, project_id, redactor = state.workspace_fs, report["project"]["id"], state.redactor
    selection, data = artifact.select(fs.list_files(project_id), lambda p: fs.read_bytes(project_id, p), redactor)
    if not selection.included:
        return BuildResult(run["state"], cost, reason="no file of the project can be packaged")
    name = artifact.archive_name(report)
    zip_bytes = artifact.build_zip(report, selection, data, redactor, when=datetime.now(UTC), extra={HOW_TO_RUN: how_to_run(report, [e.path for e in selection.included])})
    output.mkdir(parents=True, exist_ok=True)
    zip_path = output / f"{name}.zip"
    zip_path.write_bytes(zip_bytes)
    folder = output / name
    for entry in selection.included:  # the same files the ZIP holds: never an excluded secret
        target = folder / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data[entry.path])
    return BuildResult(run["state"], cost, zip_path=zip_path, folder=folder, verification=artifact.verification_level(report))


_INTENT_WORDS = {
    "adapt": "Start from this existing project and adapt it to the request. Keep its working files, structure and assets; change only what the request needs.",
    "modify": "Modify this existing project as the request asks. Keep everything the request does not ask to change.",
    "rebuild": "Rebuild this project as the request asks. Reuse its assets, content and any files that still fit; replace what the request asks to replace.",
}
MAX_REQUIREMENT = 20_000  # the API's limit for requirement_text


def source_context(source: Checkout, intent: str, *, room: int = 6_000) -> str:
    """What the planner is told about an imported repository: facts from Python (its URL, commit and file names), never text
    from inside the repository, which is untrusted."""
    files = sorted(source.files)
    head = (f"\n\nExisting project: {source.info.url} at commit {source.commit}, already imported into the workspace ({len(files)} files). "
            f"{_INTENT_WORDS.get(intent, _INTENT_WORDS['adapt'])}\nFiles:")  # fmt: skip
    listed: list[str] = []
    for name in files:
        if len(head) + sum(len(x) + 3 for x in listed) + len(name) + 40 > room:
            listed.append(f"… and {len(files) - len(listed)} more")
            break
        listed.append(name)
    return head + "".join(f"\n  {x}" for x in listed)


def import_source(session: Session, project_id: str, run_id: str, source: Checkout) -> int:
    """The pinned commit's files into the project's workspace, through the jail; nothing existing is overwritten, nothing is run.
    The import is recorded on the run like the engine's own imports, so the report names its source and commit and delivery
    requires its notices file."""
    state = session.app.state
    record = import_files(state.workspace_fs, project_id, source.files, full_name=source.info.full_name, url=source.info.url, commit=source.commit,
                          license=source.info.license or "not stated", attribution="the person's own repository, supplied by them for this build", source="github")  # fmt: skip
    with state.session_factory.begin() as db:
        state.events.record(db, project_id=project_id, run_id=run_id, type="repository.imported", payload={**record.to_dict(), "component": None, "by": "the person, before the run"})
    return len(record.files)


def run_build(session: Session, prompt: str, stack: str, cap: float, output: Path, io: IO, *, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
              source: Checkout | None = None, intent: str = "new") -> BuildResult:  # fmt: skip
    requirement = f"{prompt.strip()}\n\nTechnology stack selected by the user: {stack}. Use this stack exactly."
    if source is not None:
        requirement += source_context(source, intent, room=max(500, MAX_REQUIREMENT - len(requirement)))
    project = _json(session.client.post(f"{API}/projects", json={"name": project_name(prompt), "requirement_text": requirement[:MAX_REQUIREMENT], "stack": stack}, headers=session.headers), "saving the project")
    run = _json(session.client.post(f"{API}/projects/{project['id']}/runs", headers=session.headers), "creating the run")
    if source is not None:  # before the run starts, so its disposable copy holds the repository
        try:
            count = import_source(session, project["id"], run["id"], source)
        except ImportRefused as exc:
            raise BuildError(f"the repository could not be imported: {exc}") from exc
        io.say(f"Imported {count} file(s) from {source.info.full_name} at {source.commit[:12]} into the project (nothing in it was run).")
    _json(session.client.post(f"{API}/runs/{run['id']}/start", json={"approved_max_usd": cap}, headers=session.headers), "starting the build")
    io.say("\nBuild started. Progress:")
    try:
        final = _wait(session, run["id"], io, sleep=sleep, clock=clock)
    except KeyboardInterrupt:
        io.say("\nStopping the build…")
        final = _cancel(session, run["id"])
        return BuildResult(final["state"], final.get("cost_usd"), reason="you stopped it")
    result = package(session, final, output, io)
    if not result.ok and final["state"] != "COMPLETED":
        result.reason = final.get("status_detail") or final.get("outcome") or result.reason
    return result


def load_web_approval(paths: Paths, io: IO | None = None) -> str | None:
    """Approve the self-tested website verifier for this process, if it was set up and is unchanged. Its absence is not an error."""
    if not paths.approved_web_runtime.is_file():
        return None
    image_id, why = webruntime.load_approval(paths, web.WebVerifier(sources.egress(), work_dir=paths.work))
    if image_id is None and io is not None:
        io.say(f"Website verifier: {why}")
    return image_id


def summarise(result: BuildResult, io: IO) -> None:
    cost = f"${result.cost_usd:.4f}" if isinstance(result.cost_usd, int | float) else "unknown"
    io.say(f"\nFinal state: {result.state}. Recorded cost: {cost} (your OpenAI invoice is the authority).")
    if result.ok:
        how = "its own generated tests passed in Docker (no independent acceptance suite)" if result.verification == "generated-tests" else "independent acceptance passed"
        io.say(f"Verified: {how}.\n  ZIP:    {result.zip_path}\n  Folder: {result.folder}")
    else:
        io.say(f"No ZIP was made: {result.reason}")


# --- The command ------------------------------------------------------------------------------------------


EngineFactory = Callable[[Paths, UserConfig, str, str, Path, float, IO], Any]


def _default_engine(paths: Paths, config: UserConfig, key: str, image_id: str, manifest: Path, cap: float, io: IO) -> Any:
    return LiveEngine(paths, config, key=key, image_id=image_id, manifest=manifest, cap_usd=cap, say=io.say)


def build_command(
    paths: Paths,
    config: UserConfig,
    *,
    prompt: str | None,
    stack: str | None,
    max_usd: float | None,
    output: Path | None,
    approve_spend: float | None,
    io: IO | None = None,
    engine_factory: EngineFactory = _default_engine,
    interactive: bool = True,
    ref: str | None = None,
    look: Callable[[Paths, Any, Callable[[str], None]], Any] = sources.access,
    fetch: Callable[[Any, Any], Checkout] = sources.fetch,
) -> int:
    io = io or ConsoleIO()
    cap = max_usd if max_usd is not None else config.run_cap_usd
    if not 0 < cap <= MAX_CAP_USD:
        io.say(f"Refused: the spending limit must be above 0 and at most ${MAX_CAP_USD:.2f}.")
        return 2

    io.say("Checking this computer…")
    checks = run_checks(paths, config)
    bad = blockers(checks)
    if bad:
        io.say(render(checks))
        io.say("\nNothing was started or spent. Fix the items marked FAIL (run `devpilot doctor` to check again).")
        return 1
    io.say("  ready.")

    text = prompt
    while True:
        if text is None:
            if not interactive:
                io.say("Refused: no application description was given.")
                return 2
            text = io.ask("\nWhat application should DevPilot build? ")
        problem = validate_prompt(text) or unsupported_request(text)  # a request naming a repository is decided after reading it
        if problem is None:
            break
        io.say(problem)
        if prompt is not None or not interactive:
            return 2
        text = None

    assert text is not None  # the loop above only exits with a valid description
    try:
        request = classify(text)
        repo = request.repo.with_ref(ref) if request.repo is not None and ref is not None else request.repo
    except RepoUrlError as exc:
        io.say(f"Refused: {exc}.\nNothing was started or spent.")
        return 2
    if ref is not None and repo is None:
        io.say("Refused: --branch/--commit was given, but the request names no GitHub repository.")
        return 2
    gh = remote = None
    if repo is not None:
        found = look(paths, repo, io.say)
        if found is None:
            io.say("\nNothing was started or spent.")
            return 2
        gh, remote = found
        io.say(remote.describe())
    load_web_approval(paths, io)
    decision = decide(request, remote.profile if remote is not None else None, chosen_stack=stack)
    if not decision.accepted or decision.stack is None:
        io.say(f"\nNot built: {decision.reason}.\nNothing was started or spent.")
        return 2
    chosen = decision.stack
    io.say(f"\nStack: {chosen} ({decision.stack_name})" + (f"; starting from the repository ({request.intent})" if remote is not None else ""))
    if not confirm_spend(config, cap, io, approve_spend):
        io.say("\nNot approved: nothing was started, contacted or spent.")
        return 0

    key = credentials.load_key(paths.credentials)
    image_id, why = runtime.load_approval(paths)
    if key is None or image_id is None:  # the checks above passed, so this is a race (the key or image changed): refuse, do not guess
        io.say(f"Refused: {why or 'the OpenAI key is missing'}.")
        return 1
    out_dir = output or (Path(config.output_dir) if config.output_dir else Path.cwd() / "devpilot-output")
    source = None
    if remote is not None:
        io.say(f"Downloading {remote.info.full_name} at {remote.commit[:12]} (free; nothing in it is run)…")
        try:
            source = fetch(gh, remote)
        except RepoAccessError as exc:
            io.say(f"\nNothing was spent. {sources.explain(exc, has_token=bool(gh and gh.authorized))}")
            return 1
    try:
        with engine_factory(paths, config, key, image_id, runtime.opencode_manifest(paths), cap, io) as session:
            result = run_build(session, text, chosen, cap, out_dir, io, source=source, intent=request.intent)
    except EngineRefused as exc:
        io.say(f"\nNothing was spent. {exc}")
        return 1
    except BuildError as exc:
        io.say(f"\nThe engine refused: {exc}")
        return 1
    summarise(result, io)
    return 0 if result.ok else 1
