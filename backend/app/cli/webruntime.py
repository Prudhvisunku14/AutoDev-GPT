"""`devpilot setup --web`: build the website verification image, prove it on known sites, and only then approve it here.

The self-test runs every project in `web_fixtures.SELF_TEST` through the real verifier on the real image: the working
sites must pass and the broken ones must fail, each for the expected reason. Only then is the image's exact ID recorded
in `approved-web-runtime.json`. Each later use re-reads the tag's ID and refuses on any difference, so a rebuilt or
replaced image is not trusted until it passes again. No OpenAI request is made; the install step reaches only the npm
registry, through the install proxy.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.cli.paths import Paths
from app.core.egress import EgressPolicy
from app.verification import browser_acceptance, web, web_fixtures

Runner = Callable[..., subprocess.CompletedProcess[str]]


class WebRuntimeError(Exception):
    pass


def build_image(*, docker: str = "docker", run: Runner = subprocess.run, say: Callable[[str], None] = print) -> None:
    say(f"Building {web.WEB_IMAGE_TAG} (the first time downloads Microsoft's Playwright image, about 2 GB)…")
    done = run([docker, "build", "--pull=false", "-t", web.WEB_IMAGE_TAG, str(web.IMAGE_CONTEXT)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3_600)
    if done.returncode != 0:
        tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-8:])
        raise WebRuntimeError(f"docker build failed:\n{tail}")


def self_test(verifier: web.WebVerifier, *, say: Callable[[str], None] = print) -> list[dict[str, Any]]:
    """Every fixture through the real verifier. Raises if any result is not what it must be."""
    results = []
    for label, kind, project, must_pass, failing_stage in web_fixtures.SELF_TEST:
        say(f"  self-test: {label} (must {'pass' if must_pass else 'fail'})…")
        result = verifier.verify(web_fixtures.files(project), kind, require_approval=False)
        results.append({"fixture": label, "kind": kind, "must_pass": must_pass, "verdict": result.verdict, "reason": result.reason})
        say(f"    {result.verdict}: {result.reason}")
        if must_pass and not result.passed:
            raise WebRuntimeError(f"the verifier failed a working {label}: {result.reason}\n{result.describe()}\n{result.output_tail[-1500:]}")
        if not must_pass and result.verdict != "FAILED":
            raise WebRuntimeError(f"the verifier did not fail a broken {label} (it said {result.verdict}: {result.reason})")
        if not must_pass and not result.reason.startswith(f"{failing_stage}:"):
            raise WebRuntimeError(f"the verifier failed the broken {label} for the wrong reason (expected the {failing_stage} stage): {result.reason}")
    data, sha = browser_acceptance.freeze(web_fixtures.ACCEPTANCE_CHECKS)
    checks = browser_acceptance.thaw(data, sha)  # the same path a run's frozen checks take
    for label, project, must_pass, must_fail in web_fixtures.ACCEPTANCE_SELF_TEST:
        say(f"  self-test: {label} (must {'pass' if must_pass else 'fail ' + ', '.join(must_fail)})…")
        result = verifier.verify(web_fixtures.files(project), "static-html", require_approval=False, expect=checks.expect())
        record = browser_acceptance.judge(checks, result.acceptance, smoke_passed=result.smoke_passed, smoke_reason=result.reason)
        failed = tuple(c["code"] for c in record["criteria"] if c["status"] != browser_acceptance.PASSED)
        results.append({"fixture": label, "kind": "static-html", "must_pass": must_pass, "verdict": record["verdict"], "reason": record["reason"], "failed": list(failed)})
        say(f"    {record['verdict']}: {record['reason']}")
        if must_pass and (record["verdict"] != browser_acceptance.PASSED or not result.passed):
            details = "; ".join(f"{c['code']}: {c['detail']}" for c in record["criteria"] if c["status"] != browser_acceptance.PASSED)
            raise WebRuntimeError(f"the verifier failed the acceptance checks of a working {label}: {record['reason']} ({details})\n{result.describe()}")
        if not must_pass and (failed != must_fail or not result.smoke_passed or result.verdict != "FAILED"):
            raise WebRuntimeError(f"the broken {label} was judged wrongly: expected exactly {', '.join(must_fail)} to fail with the built-in checks passing; "
                                  f"got failed={', '.join(failed) or 'none'}, smoke_passed={result.smoke_passed}, verdict={result.verdict}")  # fmt: skip
    return results


def approve(paths: Paths, image_id: str, results: list[dict[str, Any]]) -> None:
    record = {"image_id": image_id, "tag": web.WEB_IMAGE_TAG, "approved_at": datetime.now(UTC).isoformat(timespec="seconds"), "self_test": results}
    paths.approved_web_runtime.parent.mkdir(parents=True, exist_ok=True)
    paths.approved_web_runtime.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def load_approval(paths: Paths, verifier: web.WebVerifier) -> tuple[str | None, str]:
    """(approved image ID, None-or-reason). On success the ID is approved for this process."""
    if not paths.approved_web_runtime.is_file():
        return None, "the website verification image has not been built and self-tested here: run `devpilot setup --web`"
    try:
        record = json.loads(paths.approved_web_runtime.read_text(encoding="utf-8"))
        recorded = str(record["image_id"])
    except (OSError, ValueError, KeyError):
        return None, f"{paths.approved_web_runtime} cannot be read: run `devpilot setup --web` again"
    try:
        current = verifier.image_id()
    except web.WebVerificationError as exc:
        return None, str(exc)
    if current != recorded:
        return None, f"{web.WEB_IMAGE_TAG} is not the image that passed its self-test (it was rebuilt or replaced): run `devpilot setup --web` again"
    web.approve_locally(current)
    return current, "self-tested and approved on this computer"


def setup_web(paths: Paths, egress: EgressPolicy, *, docker: str = "docker", run: Runner = subprocess.run, rebuild: bool = False, say: Callable[[str], None] = print) -> str:
    verifier = web.WebVerifier(egress, docker=docker, run=run, work_dir=paths.work)
    verifier.install_hosts()  # the allowlist must permit the registry before anything is built
    present = True
    try:
        verifier.image_id()
    except web.WebVerificationError:
        present = False
    if rebuild or not present:
        build_image(docker=docker, run=run, say=say)
    image_id = verifier.image_id()
    say(f"Self-testing {web.WEB_IMAGE_TAG} ({image_id[:19]}…) on known working and broken sites:")
    results = self_test(verifier, say=say)
    approve(paths, image_id, results)
    web.approve_locally(image_id)
    say("The website verifier passed its self-test and is approved on this computer.")
    return image_id
