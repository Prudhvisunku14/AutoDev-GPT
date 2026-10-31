"""The production OpenCode container: one hardened definition with no switch to weaken it (SECURITY T3, T4).

Nothing here starts a container. `build_production_args` gives the `docker run` arguments, after
`production_problems` has checked every host folder, the image, the binary and the managed
configuration. `deployment_problems` then confirms from `docker inspect` that they were applied.

What cannot be changed by a caller:
- The environment is fixed. `production_environment()` takes no argument, always carries
  OPENCODE_DISABLE_PROJECT_CONFIG=1, and refuses to return without it.
- `ProductionContainer` has no field for extra variables, mounts, ports or networks.
- The precedence experiment, which needs the flag off, lives only in opencode_check.py. Nothing
  in the application imports that module.

The project folder OpenCode sees is a private copy (`work_copy`), never the real workspace, so a
misbehaving engine can only damage a copy that DevPilot audits before taking anything from it.

Installation paths (`INSTALL_PATHS`) were read from the pinned 1.19.5 binary as bytes, never by
running it. Each one states what is really known: an attempt that cannot happen is different from
an attempt that happens and fails, and both differ from an attempt nobody saw.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.coding.audit import preexisting_violations, take_snapshot
from app.coding.opencode_artifact import ArtifactError, verify_binary
from app.coding.opencode_contract import CONTRACT_VERSION
from app.coding.opencode_policy import GATEWAY_KEY_PLACEHOLDER, GATEWAY_TOKEN_PREFIX, config_problems
from app.config import REPO_ROOT, Settings

# The binary of opencode-linux-x64-musl 1.19.5, recorded when it was verified (docs/PROGRESS.md).
PINNED_BINARY_SHA256 = "528058cf8f2a7196ec94dc113052b785f8f94f6802ced3980ad7359369ddacc8"
# Image IDs approved to run OpenCode. This is devpilot/opencode-runtime:1.19.5: the verified check image
# plus one file, ripgrep 15.1.0 at /usr/local/bin/rg (archive SHA-256 1c9297be...91599), built with no network.
APPROVED_RUNTIME_IMAGES: frozenset[str] = frozenset({"sha256:a7753fe443915eed28c577c1d1b3f0e27ab1c5bd16b7debc7de9579d2aaefe99"})

CONTAINER_LABEL = "devpilot.opencode"
RUN_USER = "65534:65534"  # "nobody": numeric, so the image needs no passwd entry
MEMORY = "1g"
MEMORY_BYTES = 1024**3
NANO_CPUS = 1_000_000_000
PIDS_LIMIT = 256
TMPFS_SIZE = "256m"
PASSWORD_ENV = "OPENCODE_SERVER_PASSWORD"
BINARY_TARGET = "/opt/opencode"
CONFIG_TARGET = "/etc/opencode"
WORKSPACE_TARGET = "/workspace"
SERVER_ARGV = (f"{BINARY_TARGET}/opencode", "serve", "--hostname", "127.0.0.1", "--port", "4096", "--print-logs")
# Internal Docker networks have no route out. OpenCode's only peers are the API filter and the gateway.
CORE_NETWORK = "devpilot-core"
MODEL_NETWORK = "devpilot-model"
PRODUCTION_SERVER_ARGV = (f"{BINARY_TARGET}/opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096", "--print-logs")
# Fixed and secret-free. Each OPENCODE_* name below appears in the pinned binary's flag code.
SERVER_ENV: tuple[tuple[str, str], ...] = (
    ("HOME", "/tmp/home"),
    ("XDG_CONFIG_HOME", "/tmp/home/.config"),
    ("XDG_DATA_HOME", "/tmp/home/.local/share"),
    ("XDG_CACHE_HOME", "/tmp/home/.cache"),
    ("XDG_STATE_HOME", "/tmp/home/.local/state"),
    ("TMPDIR", "/tmp"),
    ("BUN_RUNTIME_TRANSPILER_CACHE_PATH", "0"),
    ("OPENCODE_DISABLE_AUTOUPDATE", "1"),
    ("OPENCODE_DISABLE_MODELS_FETCH", "1"),
    ("OPENCODE_DISABLE_LSP_DOWNLOAD", "1"),
    ("OPENCODE_DISABLE_DEFAULT_PLUGINS", "1"),
    ("OPENCODE_PURE", "1"),
    ("OPENCODE_DISABLE_CLAUDE_CODE", "1"),
    ("OPENCODE_DISABLE_EXTERNAL_SKILLS", "1"),
)
PROJECT_CONFIG_OFF = ("OPENCODE_DISABLE_PROJECT_CONFIG", "1")
# The install guard (read from the 1.19.5 binary). For every config folder OpenCode first writes a
# .gitignore unless one exists (Config.ensureGitignore), then starts an in-process npm install of
# @opencode-ai/plugin unless the folder is not writable (Npm.install returns at once). A read-only
# folder that already holds that .gitignore therefore gets neither a write nor an install. The global
# config folder moves out of /tmp/home, because a mount inside it would make /tmp/home root-owned.
GUARD_CONFIG_HOME = "/opt/devpilot/xdg-config"
GLOBAL_CONFIG_TARGET = f"{GUARD_CONFIG_HOME}/opencode"
OPENCODE_GITIGNORE = "node_modules\npackage.json\npackage-lock.json\nbun.lock\n.gitignore\n"
INSTALL_ARTIFACTS = frozenset({"node_modules", "package.json", "package-lock.json", "bun.lock", "bun.lockb"})
# Variables the base image defines. Anything else in a container's environment is refused.
IMAGE_ENV_NAMES = frozenset({"PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256"})
UNSAFE_MOUNT_CHARS = frozenset(',"\'\r\n=')
SECRET_NAME_PARTS = ("OPENAI", "ANTHROPIC", "API_KEY", "TOKEN", "SECRET", "AWS_", "GITHUB", "GOOGLE", "DEVPILOT")
_CONTAINER_NAME = re.compile(r"^devpilot-opencode-[a-z0-9][a-z0-9-]{0,48}$")


class ContainerPolicyError(Exception):
    """The container must not start. Nothing was started. The message holds no secret value."""


# --- What is known about each installation path ---------------------------------------------------


class InstallControl(StrEnum):
    ATTEMPT_PREVENTED = "attempt prevented"  # the code path ends before it contacts anything or writes anything
    ARTIFACT_PREVENTED = "artifact prevented"  # the attempt can happen; it fails before leaving anything behind


class Evidence(StrEnum):
    OBSERVED = "observed on the real server"
    BINARY = "read from the pinned binary, not exercised"


@dataclass(frozen=True)
class InstallPath:
    name: str
    trigger: str
    control: InstallControl
    mechanism: str
    evidence: Evidence
    # True when the production builder relies on a precondition that does not hold for every image.
    needs_runtime_image: bool = False


INSTALL_PATHS: tuple[InstallPath, ...] = (
    InstallPath(
        "global config folder (Npm.install of @opencode-ai/plugin)", "server start",
        InstallControl.ATTEMPT_PREVENTED,
        "a read-only folder holding only OpenCode's .gitignore: Npm.install returns before arborist runs",
        Evidence.OBSERVED,
    ),
    InstallPath(
        "project .opencode/ folder (same install)", "server start, when the folder exists",
        InstallControl.ATTEMPT_PREVENTED,
        "OPENCODE_DISABLE_PROJECT_CONFIG=1 removes it from ConfigPaths.directories; a work copy holding it is refused",
        Evidence.OBSERVED,
    ),
    InstallPath(
        "~/.opencode folder (same install)", "server start, when the folder exists",
        InstallControl.ATTEMPT_PREVENTED,
        "HOME is a fresh tmpfs without it, external_directory is denied, and C16 fails if it appears",
        Evidence.OBSERVED,
    ),
    InstallPath(
        "provider SDK package (Npm.add)", "first use of a model whose SDK is not bundled",
        InstallControl.ATTEMPT_PREVENTED,
        "@ai-sdk/openai is in the bundled table, which is checked first; config_problems refuses any npm override and "
        "C17 reads each model's package from GET /provider",
        Evidence.BINARY,
    ),
    InstallPath(
        "plugin package (Npm.add)", "a plugin entry in the configuration",
        InstallControl.ATTEMPT_PREVENTED,
        "plugin is [] (C8), OPENCODE_PURE=1 and OPENCODE_DISABLE_DEFAULT_PLUGINS=1",
        Evidence.BINARY,
    ),
    InstallPath(
        "ripgrep download from github.com", "first use of the grep or glob tool, when `rg` is missing",
        InstallControl.ARTIFACT_PREVENTED,
        "the check image has no `rg`, so the download is attempted and fails only because there is no network. "
        "The production builder therefore refuses any image without `rg`; none is approved yet",
        Evidence.BINARY,
        needs_runtime_image=True,
    ),
    InstallPath(
        "fff native search library (libfff_c)", "file search, when the library is present",
        InstallControl.ATTEMPT_PREVENTED,
        "looked up only on the local disk beside the binary; when absent, OpenCode falls back to ripgrep. No download path exists for it",
        Evidence.BINARY,
    ),
    InstallPath(
        "language server download", "opening a file of a supported language",
        InstallControl.ATTEMPT_PREVENTED, "lsp is false (C8) and OPENCODE_DISABLE_LSP_DOWNLOAD=1", Evidence.BINARY,
    ),
    InstallPath(
        "model catalog fetch", "server start", InstallControl.ATTEMPT_PREVENTED,
        "OPENCODE_DISABLE_MODELS_FETCH=1", Evidence.BINARY,
    ),
    InstallPath(
        "self-update", "server start", InstallControl.ATTEMPT_PREVENTED,
        "autoupdate is false (C8) and OPENCODE_DISABLE_AUTOUPDATE=1", Evidence.BINARY,
    ),
    InstallPath(
        "skills fetched from a URL", "a skills.urls entry in the configuration", InstallControl.ATTEMPT_PREVENTED,
        "config_problems refuses skills.urls, and the skill permission is denied", Evidence.BINARY,
    ),
    InstallPath(
        "remote configuration (.well-known/opencode)", "a wellknown entry in OpenCode's auth store",
        InstallControl.ATTEMPT_PREVENTED,
        "the data folder is a fresh tmpfs with no auth store, and OPENCODE_AUTH_CONTENT is refused by the environment allowlist",
        Evidence.BINARY,
    ),
)  # fmt: skip


def unresolved_install_paths() -> list[InstallPath]:
    """Paths where an attempt can still happen, so only something else stops it."""
    return [p for p in INSTALL_PATHS if p.control is not InstallControl.ATTEMPT_PREVENTED]


# --- The definition -------------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeImage:
    image_id: str  # sha256:..., run by ID so a re-tagged image cannot take its place
    ripgrep: str  # where `rg` is on the image's PATH; empty when it has none


@dataclass(frozen=True)
class VerifiedBinary:
    directory: Path  # holds only the `opencode` binary
    sha256: str


@dataclass(frozen=True)
class ProductionContainer:
    name: str
    image: RuntimeImage
    binary: VerifiedBinary
    work_root: Path  # holds the three folders below; never overlaps a real workspace
    config_dir: Path  # holds only the managed opencode.json
    global_config_dir: Path  # holds only OPENCODE_GITIGNORE: the install guard
    work_copy: Path  # a private copy of the project: the only writable mount

    def mounts(self) -> tuple[tuple[Path, str, bool], ...]:
        """(host folder, container path, read-only)."""
        return (
            (self.binary.directory, BINARY_TARGET, True),
            (self.config_dir, CONFIG_TARGET, True),
            (self.global_config_dir, GLOBAL_CONFIG_TARGET, True),
            (self.work_copy, WORKSPACE_TARGET, False),
        )


def production_environment() -> tuple[tuple[str, str], ...]:
    """The container's whole environment, apart from the password passed by name. Takes no argument on purpose."""
    env = tuple((n, GUARD_CONFIG_HOME if n == "XDG_CONFIG_HOME" else v) for n, v in SERVER_ENV) + (PROJECT_CONFIG_OFF,)
    if dict(env).get("OPENCODE_DISABLE_PROJECT_CONFIG") != "1":  # read as docker would: the last value wins
        raise ContainerPolicyError("OPENCODE_DISABLE_PROJECT_CONFIG=1 is missing from the production environment")
    return env


def allowed_env_names() -> frozenset[str]:
    return frozenset({name for name, _ in production_environment()} | {PASSWORD_ENV} | IMAGE_ENV_NAMES)


def isolation_flags() -> list[str]:
    """Every hardening flag, in one place, as the no-network check container runs them."""
    return [
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={TMPFS_SIZE},mode=1777",
        "--user", RUN_USER,
        "--memory", MEMORY,
        "--memory-swap", MEMORY,
        "--cpus", "1",
        "--pids-limit", str(PIDS_LIMIT),
        "--ulimit", "nofile=1024:1024",
        "--init",
    ]  # fmt: skip


def production_flags() -> list[str]:
    """The same flags, with the two internal networks in place of no network at all."""
    flags = isolation_flags()
    at = flags.index("--network")
    flags[at : at + 2] = ["--network", CORE_NETWORK, "--network", MODEL_NETWORK]
    return flags


def verified_binary(manifest_path: Path) -> VerifiedBinary:
    """The pinned binary, after re-checking its size and hash. A different release is refused."""
    try:
        manifest, binary = verify_binary(manifest_path)
    except ArtifactError as exc:
        raise ContainerPolicyError(str(exc)) from exc
    if manifest.version != CONTRACT_VERSION or manifest.binary_sha256 != PINNED_BINARY_SHA256:
        raise ContainerPolicyError(f"the binary is not the pinned OpenCode {CONTRACT_VERSION}")
    if sorted(p.name for p in binary.parent.iterdir()) != ["opencode"]:
        raise ContainerPolicyError(f"{binary.parent} must hold only the binary")
    return VerifiedBinary(directory=binary.parent, sha256=manifest.binary_sha256)


def protected_paths(settings: Settings) -> tuple[Path, ...]:
    """Host folders that must never reach a container: the real workspaces and everything around them."""
    return (REPO_ROOT, Path.home(), settings.workspaces_dir, settings.coding_checkpoints_dir)


def host_folder_problems(host: Path, target: str, protected: Iterable[Path]) -> list[str]:
    """Why this host folder must not be mounted at `target`. Empty means it may be."""
    if any(ch in UNSAFE_MOUNT_CHARS for ch in str(host)):
        return [f"{target}: the host path has characters that cannot be mounted safely"]
    if not host.is_absolute() or host.is_symlink() or not host.is_dir():
        return [f"{target}: the host path is not an existing, absolute, non-link folder"]
    resolved = host.resolve()
    problems: list[str] = []
    for ban in (p.resolve() for p in protected):
        if resolved == ban or ban in resolved.parents or resolved in ban.parents:
            problems.append(f"{target}: {resolved} overlaps a protected folder ({ban})")
    return problems


def production_problems(c: ProductionContainer, settings: Settings) -> list[str]:
    """Everything that stops this container from starting. Empty means it may start."""
    problems: list[str] = []
    if not _CONTAINER_NAME.fullmatch(c.name):
        problems.append("the container name is not a DevPilot OpenCode name")
    if c.image.image_id not in APPROVED_RUNTIME_IMAGES:
        problems.append("the image is not an approved OpenCode runtime image")
    if not c.image.ripgrep:
        problems.append("the image has no ripgrep, so OpenCode would try to download it")
    if c.binary.sha256 != PINNED_BINARY_SHA256:
        problems.append(f"the binary is not the pinned OpenCode {CONTRACT_VERSION}")
    protected = protected_paths(settings)
    root = c.work_root.resolve()
    sources: list[Path] = []
    for host, target, _ in c.mounts():
        found = host_folder_problems(host, target, protected)
        problems += found
        if found:
            continue
        resolved = host.resolve()
        sources.append(resolved)
        if target != BINARY_TARGET and root not in resolved.parents:
            problems.append(f"{target}: must be a fresh folder under {root}")
    if len(set(sources)) != len(sources):
        problems.append("two mounts share a host folder")
    if problems:
        return problems  # the folders below may not exist or may not be DevPilot's
    return (
        _binary_folder_problems(c.binary.directory)
        + _guard_folder_problems(c.global_config_dir)
        + _config_folder_problems(c.config_dir, settings.secret_values())
        + _work_copy_problems(c.work_copy)
    )


def _binary_folder_problems(folder: Path) -> list[str]:
    return [] if sorted(p.name for p in folder.iterdir()) == ["opencode"] else [f"{BINARY_TARGET}: the folder must hold only the binary"]


def _guard_folder_problems(folder: Path) -> list[str]:
    if sorted(p.name for p in folder.iterdir()) != [".gitignore"]:
        return [f"{GLOBAL_CONFIG_TARGET}: the guard folder must hold only .gitignore"]
    if (folder / ".gitignore").read_text(encoding="utf-8") != OPENCODE_GITIGNORE:
        return [f"{GLOBAL_CONFIG_TARGET}: the .gitignore is not the one OpenCode expects, so it could rewrite it"]
    return []


def _config_folder_problems(folder: Path, secrets: list[str]) -> list[str]:
    if sorted(p.name for p in folder.iterdir()) != ["opencode.json"]:
        return [f"{CONFIG_TARGET}: the folder must hold only opencode.json"]
    try:
        config = json.loads((folder / "opencode.json").read_text(encoding="utf-8"))
    except ValueError:
        return [f"{CONFIG_TARGET}: opencode.json is not valid JSON"]
    return managed_config_problems(config, secrets=secrets)


def managed_config_problems(config: Any, *, secrets: Iterable[str]) -> list[str]:
    """Why this file must not be mounted as the managed configuration. It never echoes a value."""
    problems = config_problems(config)
    if not isinstance(config, dict):
        return problems
    text = json.dumps(config)
    if "{env:" in text or "{file:" in text:
        problems.append("the managed configuration may not read variables or files: OpenCode expands {env:...} and {file:...}")
    if any(secret and secret in text for secret in secrets):
        problems.append("the managed configuration holds a configured secret")
    options = ((config.get("provider") or {}).get("openai") or {}).get("options") or {}
    key = options.get("apiKey")
    if key != GATEWAY_KEY_PLACEHOLDER and not (isinstance(key, str) and key.startswith(GATEWAY_TOKEN_PREFIX)):
        problems.append("the managed configuration must hold only the gateway's token, never a provider key")
    return problems


def _work_copy_problems(folder: Path) -> list[str]:
    """A folder that could configure the engine is refused rather than mounted, whatever the flag does."""
    return [f"{WORKSPACE_TARGET}: {p}" for p in preexisting_violations(take_snapshot(folder))]


def build_production_args(c: ProductionContainer, settings: Settings) -> list[str]:
    """`docker run` arguments, or ContainerPolicyError. The password is passed by name only: docker reads
    its value from the CLI's own environment, so it never appears on a command line."""
    problems = production_problems(c, settings)
    if problems:
        raise ContainerPolicyError("the OpenCode container may not start: " + "; ".join(problems[:6]))
    env: list[str] = []
    for name, value in production_environment():
        env += ["--env", f"{name}={value}"]
    mounts: list[str] = []
    for host, target, read_only in c.mounts():
        mounts += ["--mount", f"type=bind,source={host},target={target}" + (",readonly" if read_only else "")]
    return [
        "run",
        "--detach",
        "--name", c.name,
        "--pull", "never",
        "--label", f"{CONTAINER_LABEL}=true",
        *production_flags(),
        "--workdir", WORKSPACE_TARGET,
        *env,
        "--env", PASSWORD_ENV,
        *mounts,
        c.image.image_id,
        *PRODUCTION_SERVER_ARGV,
    ]  # fmt: skip


# --- What `docker inspect` must show --------------------------------------------------------------


def inspect_problems(inspect: Mapping[str, Any], *, mounts: Mapping[str, bool], tmp_exec: bool = False, network: str = "none") -> list[str]:
    """Settings from `docker inspect` that differ from the hardening. `mounts`: container path to read-only."""
    host = inspect.get("HostConfig") or {}
    config = inspect.get("Config") or {}
    problems: list[str] = []
    expect = {
        "NetworkMode": network,
        "Privileged": False,
        "ReadonlyRootfs": True,
        "PublishAllPorts": False,
        "Memory": MEMORY_BYTES,
        "MemorySwap": MEMORY_BYTES,
        "NanoCpus": NANO_CPUS,
        "PidsLimit": PIDS_LIMIT,
    }
    for key, value in expect.items():
        if host.get(key) != value:
            problems.append(f"HostConfig.{key} is {host.get(key)!r}, expected {value!r}")
    if "ALL" not in [str(cap).upper() for cap in host.get("CapDrop") or []] or host.get("CapAdd"):
        problems.append("capabilities are not all dropped")
    if not any("no-new-privileges" in str(opt) for opt in host.get("SecurityOpt") or []):
        problems.append("no-new-privileges is not set")
    if host.get("PortBindings"):
        problems.append("a port is published")
    for key in ("PidMode", "IpcMode", "UTSMode", "UsernsMode"):
        if str(host.get(key) or "") == "host":
            problems.append(f"HostConfig.{key} shares the host's namespace")
    if host.get("Devices"):
        problems.append("a device is mapped")
    tmpfs = host.get("Tmpfs") or {}
    if set(tmpfs) != {"/tmp"} or ("noexec" not in tmpfs.get("/tmp", "") and not tmp_exec):
        problems.append(f"tmpfs is {tmpfs!r}, expected only /tmp with noexec")
    if config.get("User") != RUN_USER:
        problems.append(f"the user is {config.get('User')!r}, expected {RUN_USER}")
    seen: dict[str, bool] = {}
    for mount in inspect.get("Mounts") or []:
        destination = str(mount.get("Destination"))
        if "docker.sock" in str(mount.get("Source", "")) or "docker.sock" in destination:
            problems.append("the Docker socket is mounted")
        if mount.get("Type") != "bind" or destination not in mounts:
            problems.append(f"unexpected mount at {destination} ({mount.get('Type')})")
            continue
        seen[destination] = not mount.get("RW", True)
    for destination, read_only in mounts.items():
        if destination not in seen:
            problems.append(f"the mount at {destination} is missing")
        elif seen[destination] != read_only:
            problems.append(f"the mount at {destination} is {'read-only' if seen[destination] else 'writable'}")
    return problems


def environment_problems(env: Iterable[Any], *, fixed: Iterable[tuple[str, str]]) -> list[str]:
    """Problems with a container's NAME=VALUE list. Reports names only, because one of the values is the password."""
    seen = dict(str(item).partition("=")[::2] for item in env)
    problems: list[str] = []
    allowed = {name for name, _ in fixed} | {PASSWORD_ENV} | IMAGE_ENV_NAMES
    leaked = sorted(n for n in seen if n != PASSWORD_ENV and any(part in n.upper() for part in SECRET_NAME_PARTS))
    if leaked:
        problems.append(f"secret-like variables reached the container: {', '.join(leaked)}")
    unexpected = sorted(set(seen) - allowed - set(leaked))
    if unexpected:
        problems.append(f"unexpected environment variables reached the container: {', '.join(unexpected)}")
    if PASSWORD_ENV not in seen:
        problems.append("the server password variable is missing, so the server would not require one")
    return problems + [f"{name} is missing or differs from its fixed value" for name, value in fixed if seen.get(name) != value]


def deployment_problems(inspect: Mapping[str, Any], c: ProductionContainer) -> list[str]:
    """What the running production container got wrong. Empty means the definition was really applied."""
    config = inspect.get("Config") or {}
    mounts = {target: read_only for _, target, read_only in c.mounts()}
    problems = inspect_problems(inspect, mounts=mounts, network=CORE_NETWORK)
    problems += environment_problems(config.get("Env") or [], fixed=production_environment())
    if inspect.get("Image") != c.image.image_id:
        problems.append("the container does not run the approved image")
    if list(config.get("Cmd") or []) != list(PRODUCTION_SERVER_ARGV) or config.get("Entrypoint"):
        problems.append("the container's command is not the OpenCode server")
    networks = ((inspect.get("NetworkSettings") or {}).get("Networks")) or {}
    if set(networks) != {CORE_NETWORK, MODEL_NETWORK}:
        problems.append(f"the container is on {sorted(networks)}, not only the two internal networks")
    return problems


def network_problems(name: str, inspect: Mapping[str, Any] | None) -> list[str]:
    """Why this network cannot carry OpenCode's traffic. An internal network has no route out."""
    if inspect is None:
        return [f"{name} could not be read"]
    return [] if inspect.get("Internal") is True else [f"{name} is not internal, so it has a route out"]
