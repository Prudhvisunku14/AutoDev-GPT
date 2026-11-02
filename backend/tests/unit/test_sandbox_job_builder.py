"""The one job builder and the image approval check, offline: limits capped by the settings, explicit image IDs, and the
runner's own refusal of anything else. Docker is a fake (tests/docker_fake.py); nothing here starts a container."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.sandbox import policy
from app.sandbox.docker_cli import DockerCli, SandboxError, build_create_args
from app.sandbox.policy import APPROVED_IMAGES, ApprovedImage, Limits, SandboxPolicyError, approved_image, build_job, build_spec, capped
from app.sandbox.runner import DockerSandbox
from app.sandbox.spec import ExecStatus
from app.tools.adapters.workspace_fs import WorkspaceFS
from app.verification import recipes
from app.verification.results import Verdict
from tests.docker_fake import FakeDockerHost
from tests.helpers import make_settings

IMAGE_ID = "sha256:" + "ab" * 32
PROJECT_ID = "5" * 32
PYTHON = "python:3.12-alpine"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def root(settings: Settings) -> Path:
    path = settings.workspaces_dir / PROJECT_ID
    path.mkdir(parents=True)
    return path


def _value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def _limits(args: list[str]) -> tuple[str, ...]:
    return tuple(_value(args, flag) for flag in ("--memory", "--memory-swap", "--cpus", "--pids-limit"))


# --- Limits ---------------------------------------------------------------------------------------------------------


def test_a_job_gets_the_smaller_of_each_limit_and_never_more_than_the_settings(settings: Settings, root: Path) -> None:
    lower = build_job("probe", PYTHON, ("python", "-V"), root, settings, Limits("256m", 0.5, 64, 30))
    assert (lower.memory, lower.cpus, lower.pids, lower.timeout_seconds) == ("256m", 0.5, 64, 30)
    higher = build_job("probe", PYTHON, ("python", "-V"), root, settings, Limits("64g", 8, 4096, 3600))
    assert (higher.memory, higher.cpus, higher.pids, higher.timeout_seconds) == (settings.sandbox_memory, settings.sandbox_cpus, settings.sandbox_pids_limit, settings.sandbox_timeout_seconds)
    assert capped(Limits("2048m"), settings).memory == "1g" and capped(Limits("1024m"), settings).memory == "1024m"  # compared in bytes, not as text


def test_an_approved_command_keeps_exactly_the_settings_limits(settings: Settings) -> None:
    fs = WorkspaceFS(settings.workspaces_dir, max_file_bytes=1_000, max_files=10)
    fs.create_project(PROJECT_ID)
    spec = build_spec("python-unittest", None, workspace=fs, project_id=PROJECT_ID, settings=settings)
    assert (spec.memory, spec.cpus, spec.pids, spec.timeout_seconds) == (None, None, None, 120.0)
    assert _limits(build_create_args(spec, settings)) == ("1g", "1g", "1", "256")  # what every Python-only project gets today


def test_the_container_gets_the_jobs_own_limits(settings: Settings, root: Path) -> None:
    spec = build_job("probe", PYTHON, ("python", "-V"), root, settings, Limits("512m", 0.5, 64))
    assert _limits(build_create_args(spec, settings)) == ("512m", "512m", "0.5", "64")


@pytest.mark.parametrize(
    "limits",
    [{"memory": "1gb"}, {"memory": "0m"}, {"memory": 512}, {"cpus": 0}, {"cpus": -1}, {"cpus": math.nan}, {"cpus": math.inf}, {"cpus": True},
     {"pids": 8}, {"pids": 5000}, {"pids": 64.0}, {"pids": True}, {"timeout_seconds": 0}, {"timeout_seconds": -5}, {"timeout_seconds": math.nan}],
)  # fmt: skip
def test_invalid_limits_are_refused(limits: dict[str, Any]) -> None:
    with pytest.raises(SandboxPolicyError, match="invalid limits"):
        Limits(**limits)


def test_a_runtime_with_an_invalid_limit_cannot_even_be_defined() -> None:
    runtime = recipes.RECIPES["python-fastapi"].runtime
    for change in ({"memory": "lots"}, {"pids": 0}, {"cpus": 0.0}, {"timeout_seconds": -1.0}, {"network": "bridge"}, {"image": "python:latest"}, {"image": "sha256:abc"}):
        with pytest.raises(ValueError):
            replace(runtime, **change)


def test_the_runner_refuses_a_job_asking_for_more_than_the_settings(settings: Settings, root: Path) -> None:
    host = FakeDockerHost()
    sandbox = DockerSandbox(DockerCli("docker", host), settings=settings)
    base = build_job("probe", PYTHON, ("python", "-V"), root, settings)
    for greedy in (replace(base, memory="4g"), replace(base, cpus=4.0), replace(base, pids=4096), replace(base, timeout_seconds=99_999), replace(base, memory="lots")):
        with pytest.raises(SandboxError, match="more than the sandbox allows"):
            sandbox.run(greedy)
    assert host.calls == []


# --- Images ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("reference", [IMAGE_ID, PYTHON, f"{PYTHON}@sha256:{'a' * 64}", "devpilot/verify-fastapi:1"])
def test_pinned_references_and_full_image_ids_are_valid(reference: str) -> None:
    assert policy.valid_reference(reference) and ApprovedImage("x", reference).reference == reference


@pytest.mark.parametrize(
    "reference",
    ["sha256:" + "ab" * 31, "sha256:" + "AB" * 32, "sha512:" + "ab" * 64, "sha256:abc", "sha256:latest", "ab" * 32, f"{IMAGE_ID}@sha256:{'a' * 64}", "python:latest", "python"],
)
def test_anything_that_only_resembles_an_image_id_is_refused(reference: str) -> None:
    assert not policy.valid_reference(reference)
    with pytest.raises(ValueError):
        ApprovedImage("x", reference)


def test_only_an_exactly_approved_reference_runs() -> None:
    assert approved_image(PYTHON).name == "python-runner"
    for other in ("python:3.12", f"{PYTHON}@sha256:{'a' * 64}", IMAGE_ID, "devpilot/verify-fastapi:1", "devpilot/verify-react:1", "PYTHON:3.12-alpine"):
        with pytest.raises(SandboxPolicyError, match="approved list"):
            approved_image(other)


def test_the_python_runtime_stays_approved_and_the_framework_images_do_not() -> None:
    assert {name: image.reference for name, image in APPROVED_IMAGES.items()} == {"python-runner": PYTHON}
    assert [s for s, r in recipes.RECIPES.items() if r.runtime.approved] == ["python-cli"]


def test_the_builder_refuses_an_unapproved_image_and_an_empty_command(settings: Settings, root: Path) -> None:
    with pytest.raises(SandboxPolicyError, match="approved list"):
        build_job("fastapi-pytest", "devpilot/verify-fastapi:1", ("python",), root, settings)
    with pytest.raises(SandboxPolicyError, match="non-empty"):
        build_job("probe", PYTHON, (), root, settings)


def test_an_image_approved_by_id_runs_by_that_exact_id_and_no_other(settings: Settings, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(APPROVED_IMAGES, "pinned", ApprovedImage("pinned", IMAGE_ID))
    assert APPROVED_IMAGES["pinned"].immutable and not APPROVED_IMAGES["python-runner"].immutable
    host = FakeDockerHost()
    result = DockerSandbox(DockerCli("docker", host), settings=settings).run(build_job("probe", IMAGE_ID, ("python", "-V"), root, settings))
    create = next(call for call in host.calls if call[0] == "create")
    assert result.status is ExecStatus.SUCCEEDED and create[create.index(IMAGE_ID) - 1].startswith("type=bind") and _value(create, "--pull") == "never"
    with pytest.raises(SandboxPolicyError, match="approved list"):
        build_job("probe", IMAGE_ID.replace("ab", "cd", 1), ("python", "-V"), root, settings)


def test_an_approved_image_missing_from_the_disk_is_never_pulled_and_never_passes(settings: Settings, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(APPROVED_IMAGES, "pinned", ApprovedImage("pinned", IMAGE_ID))
    host = FakeDockerHost(create_error=f"Error response from daemon: No such image: {IMAGE_ID}")
    result = DockerSandbox(DockerCli("docker", host), settings=settings).run(build_job("fastapi-pytest", IMAGE_ID, ("python", "-V"), root, settings))
    assert result.status is ExecStatus.ERROR and "No such image" in (result.detail or "")
    assert [call[0] for call in host.calls] == ["version", "create"]  # no pull, no build, nothing started
    assert recipes.judge_recipe(recipes.RECIPES["python-fastapi"], result).verdict is Verdict.NOT_RUN
