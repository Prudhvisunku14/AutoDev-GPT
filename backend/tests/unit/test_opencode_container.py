"""The production OpenCode container: what it always does, and what it refuses to start with.

Nothing here starts a container. The hostile values below are plain strings in a plain dict.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest

from app import coding
from app.coding import opencode_container as container
from app.coding.opencode_check import CheckContainer, build_run_args
from app.coding.opencode_container import (
    APPROVED_RUNTIME_IMAGES,
    GLOBAL_CONFIG_TARGET,
    GUARD_CONFIG_HOME,
    INSTALL_PATHS,
    MEMORY_BYTES,
    NANO_CPUS,
    OPENCODE_GITIGNORE,
    PASSWORD_ENV,
    PIDS_LIMIT,
    PINNED_BINARY_SHA256,
    RUN_USER,
    SERVER_ARGV,
    ContainerPolicyError,
    Evidence,
    InstallControl,
    ProductionContainer,
    RuntimeImage,
    VerifiedBinary,
    allowed_env_names,
    build_production_args,
    deployment_problems,
    environment_problems,
    host_folder_problems,
    managed_config_problems,
    production_environment,
    production_problems,
    protected_paths,
    unresolved_install_paths,
    verified_binary,
)
from app.coding.opencode_policy import managed_config
from app.config import REPO_ROOT, Settings
from tests.helpers import FAKE_OPENAI_KEY, make_settings

IMAGE_ID = "sha256:" + "a" * 64
FLAG = "OPENCODE_DISABLE_PROJECT_CONFIG"
APP_ROOT = Path(coding.__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    return make_settings(tmp_path / "host")


@pytest.fixture
def approved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container, "APPROVED_RUNTIME_IMAGES", frozenset({IMAGE_ID}))


def _write_config(folder: Path) -> None:
    config = managed_config(model="openai/x", gateway_base_url="http://gw.invalid/v1")
    (folder / "opencode.json").write_text(json.dumps(config), encoding="utf-8")


def _container(root: Path) -> ProductionContainer:
    binary = root / "cache" / "bin"
    work = root / "work"
    for folder in (binary, work / "etc", work / "global", work / "copy"):
        folder.mkdir(parents=True, exist_ok=True)
    (binary / "opencode").write_bytes(b"\x7fELF")
    (work / "global" / ".gitignore").write_text(OPENCODE_GITIGNORE, encoding="utf-8")
    (work / "copy" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    _write_config(work / "etc")
    return ProductionContainer(
        name="devpilot-opencode-p1",
        image=RuntimeImage(image_id=IMAGE_ID, ripgrep="/usr/bin/rg"),
        binary=VerifiedBinary(directory=binary, sha256=PINNED_BINARY_SHA256),
        work_root=work,
        config_dir=work / "etc",
        global_config_dir=work / "global",
        work_copy=work / "copy",
    )


def _problems(c: ProductionContainer, settings: Settings) -> str:
    return " | ".join(production_problems(c, settings))


# --- The flag cannot be omitted ---------------------------------------------------------------


def test_the_flag_is_in_every_production_container(tmp_path: Path, settings: Settings, approved: None) -> None:
    args = build_production_args(_container(tmp_path), settings)
    assert f"{FLAG}=1" in args
    assert (FLAG, "1") in production_environment()


def test_there_is_no_argument_that_could_turn_the_flag_off() -> None:
    assert inspect.signature(production_environment).parameters == {}
    assert list(inspect.signature(build_production_args).parameters) == ["c", "settings"]
    fields = {f.name for f in dataclasses.fields(ProductionContainer)}
    assert fields == {"name", "image", "binary", "work_root", "config_dir", "global_config_dir", "work_copy"}


@pytest.mark.parametrize("override", ["project_config_off", "environment", "env", "extra_env", "network", "mounts", "tmp_exec", "publish"])
def test_a_caller_cannot_pass_an_override(tmp_path: Path, override: str) -> None:
    c = _container(tmp_path)
    with pytest.raises(TypeError):
        dataclasses.replace(c, **{override: False})


def test_a_flag_that_was_edited_away_stops_the_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: Settings, approved: None) -> None:
    for broken in ((FLAG, "0"), (FLAG, ""), ("SOMETHING_ELSE", "1")):
        monkeypatch.setattr(container, "PROJECT_CONFIG_OFF", broken)
        with pytest.raises(ContainerPolicyError, match=FLAG):
            production_environment()
        with pytest.raises(ContainerPolicyError, match=FLAG):
            build_production_args(_container(tmp_path), settings)


def test_a_later_entry_cannot_undo_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Docker keeps the last value given. The flag is added last, and the check reads it the same way.
    monkeypatch.setattr(container, "SERVER_ENV", ((FLAG, "0"),))
    assert dict(production_environment())[FLAG] == "1"


def test_no_application_module_can_reach_the_experiment_that_turns_it_off() -> None:
    imports = re.compile(r"^\s*(from|import) .*opencode_check\b", re.MULTILINE)
    importers = [p.name for p in APP_ROOT.rglob("*.py") if p.name != "opencode_check.py" and imports.search(p.read_text(encoding="utf-8"))]
    assert importers == []


def test_the_production_module_has_no_way_to_express_the_flag_being_off() -> None:
    source = (APP_ROOT / "coding" / "opencode_container.py").read_text(encoding="utf-8")
    assert "project_config_off" not in source
    assert source.count(f'"{FLAG}"') == 2  # the definition and its check


def test_the_baseline_runs_the_same_environment_and_flags_as_production(tmp_path: Path) -> None:
    run = tmp_path / "run"
    for name in ("etc", "checks", "workspace", "global"):
        (run / name).mkdir(parents=True)
    c = CheckContainer(
        name="x", binary_dir=run / "etc", config_dir=run / "etc", checks_dir=run / "checks",
        workspace_dir=run / "workspace", guard_config_dir=run / "global",
    )  # fmt: skip
    assert sorted(c.environment()) == sorted(production_environment())
    joined = " ".join(build_run_args(c))
    assert " ".join(container.isolation_flags()) in joined


# --- The other requirements of the definition -------------------------------------------------


def test_every_hardening_flag_is_present(tmp_path: Path, settings: Settings, approved: None) -> None:
    args = build_production_args(_container(tmp_path), settings)
    joined = " ".join(args)
    for flag in ("--network none", "--cap-drop ALL", "--security-opt no-new-privileges", "--read-only", f"--user {RUN_USER}",
                 "--memory 1g", "--memory-swap 1g", "--cpus 1", f"--pids-limit {PIDS_LIMIT}", "--ulimit nofile=1024:1024",
                 "--pull never", "--init", "/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777"):  # fmt: skip
        assert flag in joined, flag
    assert not {"-p", "--publish", "--publish-all", "--privileged", "--net", "-v", "--volume", "--env-file", "--cap-add"} & set(args)
    assert "docker.sock" not in joined
    assert RUN_USER not in ("0", "root") and not RUN_USER.startswith("0:")


def test_it_runs_the_pinned_image_by_id_and_only_the_server(tmp_path: Path, settings: Settings, approved: None) -> None:
    args = build_production_args(_container(tmp_path), settings)
    assert args[-(len(SERVER_ARGV) + 1) :] == [IMAGE_ID, *SERVER_ARGV]


def test_only_the_private_copy_is_writable(tmp_path: Path, settings: Settings, approved: None) -> None:
    mounts = [a for a in build_production_args(_container(tmp_path), settings) if a.startswith("type=bind")]
    assert len(mounts) == 4
    writable = [m for m in mounts if not m.endswith(",readonly")]
    assert len(writable) == 1 and writable[0].endswith("target=/workspace") and "copy" in writable[0]
    assert any(m.endswith(f"target={GLOBAL_CONFIG_TARGET},readonly") for m in mounts)


def test_the_password_is_the_only_secret_and_goes_by_name(tmp_path: Path, settings: Settings, approved: None) -> None:
    args = build_production_args(_container(tmp_path), settings)
    assert PASSWORD_ENV in args and not any(a.startswith(PASSWORD_ENV + "=") for a in args)
    names = {a.split("=")[0] for a in args if re.fullmatch(r"[A-Z_]+=.*", a)}
    assert names == {n for n, _ in production_environment()}
    assert not [n for n in names if any(part in n for part in container.SECRET_NAME_PARTS)]
    assert FAKE_OPENAI_KEY not in " ".join(args)
    assert f"XDG_CONFIG_HOME={GUARD_CONFIG_HOME}" in args


def test_a_good_definition_has_no_problems(tmp_path: Path, settings: Settings, approved: None) -> None:
    assert production_problems(_container(tmp_path), settings) == []


# --- Refused images and binaries --------------------------------------------------------------


def test_no_runtime_image_is_approved_yet_so_nothing_can_start(tmp_path: Path, settings: Settings) -> None:
    assert APPROVED_RUNTIME_IMAGES == frozenset()
    with pytest.raises(ContainerPolicyError, match="not an approved"):
        build_production_args(_container(tmp_path), settings)


def test_an_image_without_ripgrep_is_refused(tmp_path: Path, settings: Settings, approved: None) -> None:
    c = dataclasses.replace(_container(tmp_path), image=RuntimeImage(image_id=IMAGE_ID, ripgrep=""))
    assert "no ripgrep" in _problems(c, settings)


def test_a_binary_that_is_not_the_pinned_one_is_refused(tmp_path: Path, settings: Settings, approved: None) -> None:
    c = _container(tmp_path)
    other = dataclasses.replace(c, binary=VerifiedBinary(c.binary.directory, "0" * 64))
    assert "not the pinned" in _problems(other, settings)
    (c.binary.directory / "extra").write_text("x")
    assert "must hold only the binary" in _problems(c, settings)


def _manifest(root: Path, *, version: str = "1.19.5", sha256: str = "") -> Path:
    from app.coding.opencode_artifact import Manifest, sha256_file, write_manifest

    binary = root / "bin" / "opencode"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"\x7fELF-test-binary")
    manifest = Manifest(
        package="opencode-linux-x64-musl", version=version, metadata_url="u", tarball_url="u", tarball_file="t", tarball_bytes=1,
        integrity="i", shasum=None, tarball_sha256="x", binary_file="bin/opencode", binary_bytes=binary.stat().st_size,
        binary_sha256=sha256 or sha256_file(binary), files=1, unpacked_bytes=1, registry_file_count=None,
        registry_unpacked_size=None, elf_interpreter=None, elf_needed=(), verified_at="now",
    )  # fmt: skip
    path = root / "manifest.json"
    write_manifest(path, manifest)
    return path


def test_the_binary_is_rechecked_against_the_manifest_and_the_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.coding.opencode_artifact import sha256_file

    path = _manifest(tmp_path)
    binary = tmp_path / "bin" / "opencode"
    with pytest.raises(ContainerPolicyError, match="not the pinned"):
        verified_binary(path)  # a well-formed binary, but not the pinned one

    monkeypatch.setattr(container, "PINNED_BINARY_SHA256", sha256_file(binary))
    found = verified_binary(path)
    assert found.directory == binary.parent.resolve() and found.sha256 == sha256_file(binary)

    (binary.parent / "extra").write_text("x")
    with pytest.raises(ContainerPolicyError, match="only the binary"):
        verified_binary(path)
    (binary.parent / "extra").unlink()

    binary.write_bytes(b"\x7fELF-tampered!!!!")
    with pytest.raises(ContainerPolicyError, match="no longer matches"):
        verified_binary(path)


def test_another_release_is_refused_even_with_a_matching_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.coding.opencode_artifact import sha256_file

    path = _manifest(tmp_path, version="1.19.6")
    monkeypatch.setattr(container, "PINNED_BINARY_SHA256", sha256_file(tmp_path / "bin" / "opencode"))
    with pytest.raises(ContainerPolicyError, match="not the pinned"):
        verified_binary(path)


# --- The real workspace never reaches the container -------------------------------------------


def test_the_real_workspace_and_everything_around_it_is_protected(settings: Settings, tmp_path: Path) -> None:
    protected = protected_paths(settings)
    assert settings.workspaces_dir in protected and settings.coding_checkpoints_dir in protected
    assert REPO_ROOT in protected and tmp_path / "home" in protected


def _swap(c: ProductionContainer, field: str, path: Path) -> ProductionContainer:
    path.mkdir(parents=True, exist_ok=True)
    return dataclasses.replace(c, **{field: path})


def test_mounting_a_real_project_workspace_is_refused(tmp_path: Path, settings: Settings, approved: None) -> None:
    c = _container(tmp_path)
    real = settings.workspaces_dir / ("a" * 32)
    swapped = _swap(c, "work_copy", real)
    assert "overlaps a protected folder" in _problems(swapped, settings)
    with pytest.raises(ContainerPolicyError, match="overlaps"):
        build_production_args(swapped, settings)


@pytest.mark.parametrize("where", ["repo", "home", "checkpoints", "above"])
def test_other_protected_folders_are_refused(tmp_path: Path, settings: Settings, approved: None, where: str) -> None:
    c = _container(tmp_path)
    target = {
        "repo": REPO_ROOT / "backend",
        "home": tmp_path / "home" / "stuff",
        "checkpoints": settings.coding_checkpoints_dir / "run",
        "above": settings.workspaces_dir.parent,  # a folder that contains the real workspaces
    }[where]
    if where == "repo":
        swapped = dataclasses.replace(c, work_copy=target)
    else:
        swapped = _swap(c, "work_copy", target)
    assert "overlaps a protected folder" in _problems(swapped, settings)


def test_a_link_is_refused_as_a_mount_source(tmp_path: Path, settings: Settings, approved: None) -> None:
    c = _container(tmp_path)
    link = tmp_path / "work" / "link"
    try:
        link.symlink_to(settings.workspaces_dir.parent, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot create symbolic links")
    assert "non-link" in _problems(dataclasses.replace(c, work_copy=link), settings)


@pytest.mark.parametrize("name", ["a,b", "a=b", "a'b"])
def test_characters_that_would_change_a_mount_are_refused(tmp_path: Path, name: str) -> None:
    folder = tmp_path / name
    folder.mkdir()
    assert any("cannot be mounted safely" in p for p in host_folder_problems(folder, "/workspace", []))


def test_relative_or_missing_sources_are_refused(tmp_path: Path) -> None:
    assert any("existing, absolute" in p for p in host_folder_problems(Path("relative"), "/workspace", []))
    assert any("existing, absolute" in p for p in host_folder_problems(tmp_path / "missing", "/workspace", []))


def test_mounts_outside_the_work_root_or_sharing_a_folder_are_refused(tmp_path: Path, settings: Settings, approved: None) -> None:
    c = _container(tmp_path)
    outside = _swap(c, "work_copy", tmp_path / "elsewhere")
    assert "must be a fresh folder" in _problems(outside, settings)
    shared = dataclasses.replace(c, work_copy=c.config_dir)
    assert "share a host folder" in _problems(shared, settings)


# --- Refused project configuration ------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["opencode.json", "opencode.jsonc", ".opencode/agent/x.md", "sub/.opencode/plugin/x.js", "sub/opencode.json",
     "AGENTS.md", "CLAUDE.md", ".git/config", ".env", ".devpilot/x"],
)  # fmt: skip
def test_a_work_copy_that_could_configure_the_engine_is_refused(tmp_path: Path, settings: Settings, approved: None, path: str) -> None:
    c = _container(tmp_path)
    target = c.work_copy / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")
    assert "/workspace:" in _problems(c, settings)
    with pytest.raises(ContainerPolicyError):
        build_production_args(c, settings)


def test_a_link_inside_the_work_copy_is_refused(tmp_path: Path, settings: Settings, approved: None) -> None:
    c = _container(tmp_path)
    try:
        (c.work_copy / "escape").symlink_to(tmp_path / "home", target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot create symbolic links")
    assert "symlinks and junctions" in _problems(c, settings)


def test_the_guard_folder_must_be_exactly_what_opencode_would_leave_alone(tmp_path: Path, settings: Settings, approved: None) -> None:
    c = _container(tmp_path)
    (c.global_config_dir / "plugin.js").write_text("x")
    assert "must hold only .gitignore" in _problems(c, settings)
    (c.global_config_dir / "plugin.js").unlink()
    (c.global_config_dir / ".gitignore").write_text("different")
    assert "could rewrite it" in _problems(c, settings)
    (c.global_config_dir / ".gitignore").unlink()
    assert "must hold only .gitignore" in _problems(c, settings)


# --- No provider credentials ------------------------------------------------------------------


def _config(**changes: Any) -> dict[str, Any]:
    config = managed_config(model="openai/x", gateway_base_url="http://gw.invalid/v1")
    config.update(changes)
    return config


def test_the_managed_configuration_passes_and_holds_only_a_placeholder() -> None:
    config = _config()
    assert managed_config_problems(config, secrets=[FAKE_OPENAI_KEY]) == []
    assert FAKE_OPENAI_KEY not in json.dumps(config)


@pytest.mark.parametrize(
    ("key", "reason"),
    [("sk-real-looking-key", "gateway placeholder"), ("{env:OPENAI_API_KEY}", "read variables"), ("{file:/run/key}", "read variables")],
)
def test_a_key_or_a_way_to_fetch_one_is_refused(key: str, reason: str) -> None:
    config = _config()
    config["provider"]["openai"]["options"]["apiKey"] = key
    problems = managed_config_problems(config, secrets=[])
    assert any(reason in p for p in problems)
    assert key not in " ".join(problems)


def test_a_configured_secret_is_found_anywhere_and_never_echoed() -> None:
    config = _config(username=f"prefix-{FAKE_OPENAI_KEY}")
    problems = managed_config_problems(config, secrets=[FAKE_OPENAI_KEY])
    assert any("holds a configured secret" in p for p in problems)
    assert FAKE_OPENAI_KEY not in " ".join(problems)


def test_the_file_on_disk_is_what_is_checked(tmp_path: Path, settings: Settings, approved: None) -> None:
    c = _container(tmp_path)
    config = _config()
    config["provider"]["openai"]["options"]["apiKey"] = FAKE_OPENAI_KEY
    (c.config_dir / "opencode.json").write_text(json.dumps(config), encoding="utf-8")
    text = _problems(c, settings)
    assert "holds a configured secret" in text and FAKE_OPENAI_KEY not in text
    (c.config_dir / "opencode.json").write_text("{not json", encoding="utf-8")
    assert "not valid JSON" in _problems(c, settings)
    (c.config_dir / "opencode.json").write_text("[]", encoding="utf-8")
    assert "not a JSON object" in _problems(c, settings)
    (c.config_dir / "extra.json").write_text("{}", encoding="utf-8")
    assert "only opencode.json" in _problems(c, settings)


def test_a_provider_package_override_is_refused_in_the_managed_file() -> None:
    config = _config()
    config["provider"]["openai"]["npm"] = "evil-provider"
    assert any("install from npm: evil-provider" in p for p in managed_config_problems(config, secrets=[]))


# --- What docker inspect must show ------------------------------------------------------------


def _env(**overrides: str | None) -> list[str]:
    values = {**dict(production_environment()), PASSWORD_ENV: "s3cret-value", "PATH": "/usr/bin", "LANG": "C.UTF-8",
              "GPG_KEY": "7169", "PYTHON_VERSION": "3.12", "PYTHON_SHA256": "ab", **overrides}  # fmt: skip
    return [f"{k}={v}" for k, v in values.items() if v is not None]


def _inspect(c: ProductionContainer) -> dict[str, Any]:
    return {
        "Image": IMAGE_ID,
        "HostConfig": {
            "NetworkMode": "none", "Privileged": False, "ReadonlyRootfs": True, "PublishAllPorts": False,
            "Memory": MEMORY_BYTES, "MemorySwap": MEMORY_BYTES, "NanoCpus": NANO_CPUS, "PidsLimit": PIDS_LIMIT,
            "CapDrop": ["ALL"], "CapAdd": None, "SecurityOpt": ["no-new-privileges"], "PortBindings": {},
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=256m,mode=1777"}, "Devices": [], "PidMode": "", "IpcMode": "private",
        },
        "Config": {"User": RUN_USER, "Env": _env(), "Cmd": list(SERVER_ARGV), "Entrypoint": None},
        "Mounts": [{"Type": "bind", "Source": str(host), "Destination": target, "RW": not ro} for host, target, ro in c.mounts()],
    }  # fmt: skip


def test_a_matching_deployment_passes(tmp_path: Path) -> None:
    c = _container(tmp_path)
    assert deployment_problems(_inspect(c), c) == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda i: i["Config"].update(Env=_env(**{FLAG: "0"})), f"{FLAG} is missing or differs"),
        (lambda i: i["Config"].update(Env=_env(**{FLAG: None})), f"{FLAG} is missing or differs"),
        (lambda i: i["Config"].update(Env=_env(OPENCODE_CONFIG="/workspace/x.json")), "unexpected environment variables reached the container: OPENCODE_CONFIG"),
        (lambda i: i["Config"].update(Env=_env(OPENCODE_CONFIG_DIR="/workspace")), "unexpected environment variables"),
        (lambda i: i["Config"].update(Env=_env(OPENCODE_CONFIG_CONTENT="{}")), "unexpected environment variables"),
        (lambda i: i["Config"].update(Env=_env(OPENCODE_PERMISSION="{}")), "unexpected environment variables"),
        (lambda i: i["Config"].update(Env=_env(OPENCODE_AUTH_CONTENT="{}")), "unexpected environment variables"),
        (lambda i: i["Config"].update(Env=_env(OPENCODE_MODELS_URL="http://x")), "unexpected environment variables"),
        (lambda i: i["Config"].update(Env=_env(OTEL_EXPORTER_OTLP_ENDPOINT="http://x")), "unexpected environment variables"),
        (lambda i: i["Config"].update(Env=_env(BUN_BE_BUN="1")), "unexpected environment variables"),
        (lambda i: i["Config"].update(Env=_env(OPENAI_API_KEY="sk-x")), "secret-like variables reached the container: OPENAI_API_KEY"),
        (lambda i: i["Config"].update(Env=_env(**{PASSWORD_ENV: None})), "password variable is missing"),
        (lambda i: i["Config"].update(Env=_env(HOME="/root")), "HOME is missing or differs"),
        (lambda i: i["Config"].update(Env=_env(XDG_CONFIG_HOME="/tmp/home/.config")), "XDG_CONFIG_HOME is missing or differs"),
        (lambda i: i["HostConfig"].update(NetworkMode="bridge"), "NetworkMode"),
        (lambda i: i["HostConfig"].update(Memory=0), "Memory"),
        (lambda i: i["Config"].update(User="0:0"), "user"),
        (lambda i: i["Mounts"][3].update(RW=False), "read-only"),
        (lambda i: i["Mounts"][1].update(RW=True), "writable"),
        (lambda i: i.update(Image="sha256:other"), "approved image"),
        (lambda i: i["Config"].update(Cmd=["sh"]), "command is not the OpenCode server"),
        (lambda i: i["Config"].update(Entrypoint=["sh"]), "command is not the OpenCode server"),
    ],
)
def test_each_deviation_in_the_real_container_is_reported(tmp_path: Path, change: Any, reason: str) -> None:
    c = _container(tmp_path)
    found = _inspect(c)
    change(found)
    assert any(reason in p for p in deployment_problems(found, c)), deployment_problems(found, c)


def test_environment_problems_name_variables_but_never_show_values() -> None:
    problems = environment_problems(_env(OPENCODE_CONFIG_CONTENT="hidden-config", OPENAI_API_KEY="sk-hidden"), fixed=production_environment())
    text = " ".join(problems)
    assert "OPENCODE_CONFIG_CONTENT" in text and "OPENAI_API_KEY" in text
    assert "hidden" not in text and "s3cret-value" not in text


def test_the_allowed_names_are_exactly_the_fixed_ones_the_password_and_the_images() -> None:
    assert allowed_env_names() == {n for n, _ in production_environment()} | {PASSWORD_ENV} | container.IMAGE_ENV_NAMES
    assert not {"OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT", "OPENCODE_PERMISSION", "OPENAI_API_KEY"} & allowed_env_names()


# --- What is known about installation paths ---------------------------------------------------


def test_only_ripgrep_can_still_attempt_something_and_the_builder_refuses_it() -> None:
    unresolved = unresolved_install_paths()
    assert [p.name for p in unresolved] == ["ripgrep download from github.com"]
    assert unresolved[0].control is InstallControl.ARTIFACT_PREVENTED and unresolved[0].needs_runtime_image


def test_each_path_says_how_it_is_known_and_observed_claims_are_only_what_ran() -> None:
    assert all(p.mechanism and p.trigger for p in INSTALL_PATHS)
    observed = {p.name for p in INSTALL_PATHS if p.evidence is Evidence.OBSERVED}
    assert all("folder" in name for name in observed)  # provider, plugin and ripgrep paths never ran with a model
    assert next(p for p in INSTALL_PATHS if p.name.startswith("provider SDK")).evidence is Evidence.BINARY
