"""Application settings, loaded only from the environment or the repo-root `.env`.

Relative paths in settings resolve against the repository root, so the backend
behaves the same whatever the current working directory is.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> parents[2] is the repository root.
REPO_ROOT = Path(__file__).resolve().parents[2]

MIN_API_TOKEN_LENGTH = 32

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Validation errors must never echo values: a mistyped token would otherwise be
        # printed in the startup traceback.
        hide_input_in_errors=True,
    )

    # --- Backend ---------------------------------------------------------
    devpilot_host: str = "127.0.0.1"
    devpilot_port: int = Field(default=8000, ge=1, le=65535)
    devpilot_api_token: SecretStr
    devpilot_cors_origins: str = "http://127.0.0.1:5173"
    database_url: str = "sqlite:///./data/devpilot.db"
    workspaces_dir: Path = Path("./workspaces")
    log_level: LogLevel = "INFO"
    egress_allowlist_file: Path = Path("config/egress_allowlist.yaml")

    # --- Workspace limits (enforced by WorkspaceFS) ------------------------
    workspace_max_file_bytes: int = Field(default=2_000_000, gt=0)
    workspace_max_files: int = Field(default=5_000, gt=0)

    # --- OpenAI: GPT orchestrator (Responses API) -------------------------
    openai_model: str | None = None
    openai_base_url: str | None = None
    openai_store_responses: bool = True
    openai_timeout_seconds: float = Field(default=120.0, gt=0)
    openai_max_retries: int = Field(default=2, ge=0, le=10)
    openai_max_output_tokens: int | None = Field(default=None, gt=0)
    # USD per 1M tokens. Unset means "cost unknown": the token cap is enforced instead.
    openai_price_input_per_mtok: float | None = Field(default=None, ge=0)
    openai_price_output_per_mtok: float | None = Field(default=None, ge=0)

    # --- Run limits (enforced in code, never by prompts) ------------------
    max_turns: int = Field(default=60, gt=0)
    max_tool_calls: int = Field(default=100, gt=0)
    max_repeated_tool_calls: int = Field(default=3, gt=0)
    max_nudges: int = Field(default=2, ge=0)
    run_budget_usd: float = Field(default=2.0, gt=0)
    run_max_tokens: int = Field(default=1_000_000, gt=0)
    run_wall_clock_seconds: float = Field(default=1800, gt=0)
    tool_output_max_chars: int = Field(default=20_000, ge=1_000)

    # --- Server-sent events ------------------------------------------------
    sse_poll_seconds: float = Field(default=0.5, gt=0)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)
    sse_max_stream_seconds: float = Field(default=300.0, gt=0)

    # --- Execution sandbox (Docker CLI; SECURITY T1, T2) -------------------
    # The docker executable. Give the full path if docker.exe is not on PATH.
    docker_cli: str = Field(default="docker", min_length=1)
    sandbox_memory: str = Field(default="1g", pattern=r"^[1-9][0-9]{0,5}[bkmg]$")
    sandbox_cpus: float = Field(default=1.0, gt=0, le=8)
    sandbox_pids_limit: int = Field(default=256, ge=16, le=4096)
    sandbox_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    # Per stream. Output beyond this is dropped and marked truncated; a flood kills the container.
    sandbox_max_output_bytes: int = Field(default=65_536, ge=1_024, le=4_194_304)

    # --- OpenCode coding engine (Phase 4; SECURITY T3, T4) -----------------
    # Off by default: IMPLEMENTING is orchestrated only when the OpenCode container is running.
    opencode_enabled: bool = False
    # Must be a loopback address that the egress allowlist lists for the backend.
    opencode_base_url: str = "http://127.0.0.1:4096"
    opencode_server_username: str = Field(default="opencode", min_length=1)
    # provider/model, e.g. openai/<model-id>. Only the openai provider is accepted.
    opencode_model: str | None = None
    opencode_timeout_seconds: float = Field(default=600.0, gt=0, le=3600)
    # The project folder as the OpenCode container sees it. Every API call is pinned to it.
    opencode_workspace_dir: str = Field(default="/workspace", pattern=r"^/[A-Za-z0-9._/-]*$")
    opencode_max_tokens_per_task: int = Field(default=200_000, gt=0)
    opencode_max_attempts_per_task: int = Field(default=3, ge=1, le=10)
    # Pre-images of the workspace, kept while a coding task runs so it can be reverted.
    coding_checkpoints_dir: Path = Path("./data/checkpoints")
    # A run's disposable copy of the project. Outside the repository: the launcher refuses to mount from it.
    coding_work_dir: Path = Path("D:/DevPilotCache/work")
    # How many times a failed verification may go back to REPAIRING. Coding attempts per task are above.
    max_repair_attempts: int = Field(default=2, ge=0, le=5)

    # --- Isolated launcher: stays refused until each of these is approved -----
    # The approved runtime image's ID (sha256:...). Must also be in opencode_container.APPROVED_RUNTIME_IMAGES.
    opencode_runtime_image: str | None = None
    # The verified binary's manifest, written by scripts/fetch_opencode.py.
    opencode_manifest_file: Path | None = None
    # A person's approval of Docker networks and a real model call. Nothing else starts a live coding run.
    coding_live_approved: bool = False

    # --- Model gateway budgets, per coding attempt ------------------------------
    gateway_max_requests_per_task: int = Field(default=40, gt=0)
    gateway_max_output_tokens: int = Field(default=8_000, gt=0)
    gateway_max_usd_per_task: float | None = Field(default=None, gt=0)

    # --- Credentials used by later phases ---------------------------------
    # Declared here only so the redactor knows their values. Phase 1 never uses them.
    openai_api_key: SecretStr | None = None
    opencode_server_password: SecretStr | None = None
    n8n_api_key: SecretStr | None = None
    n8n_webhook_secret: SecretStr | None = None
    github_token: SecretStr | None = None
    google_oauth_client_secret: SecretStr | None = None
    devpilot_token_key: SecretStr | None = None

    @field_validator(
        "openai_api_key",
        "opencode_server_password",
        "n8n_api_key",
        "n8n_webhook_secret",
        "github_token",
        "google_oauth_client_secret",
        "devpilot_token_key",
        mode="before",
    )
    @classmethod
    def _empty_secret_is_unset(cls, value: object) -> object:
        # `.env.example` leaves secrets empty; treat "" as "not configured".
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator(
        "openai_model",
        "openai_base_url",
        "opencode_model",
        "opencode_runtime_image",
        "opencode_manifest_file",
        "gateway_max_usd_per_task",
        "openai_max_output_tokens",
        "openai_price_input_per_mtok",
        "openai_price_output_per_mtok",
        mode="before",
    )
    @classmethod
    def _empty_optional_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("devpilot_api_token")
    @classmethod
    def _api_token_strong_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_API_TOKEN_LENGTH:
            raise ValueError(
                f"DEVPILOT_API_TOKEN must be at least {MIN_API_TOKEN_LENGTH} characters"
            )
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("devpilot_host")
    @classmethod
    def _host_is_loopback(cls, value: str) -> str:
        # Single-user design (SECURITY T7): the API binds to loopback only.
        if value == "localhost":
            return value
        try:
            if ipaddress.ip_address(value).is_loopback:
                return value
        except ValueError:
            pass
        raise ValueError("DEVPILOT_HOST must be a loopback address (127.0.0.1, ::1 or localhost)")

    @field_validator("database_url")
    @classmethod
    def _sqlite_only(cls, value: str) -> str:
        if not value.startswith("sqlite:///"):
            raise ValueError("DATABASE_URL must be a sqlite:/// URL")
        return value

    @model_validator(mode="after")
    def _resolve_paths(self) -> Settings:
        self.workspaces_dir = _from_repo_root(self.workspaces_dir)
        self.egress_allowlist_file = _from_repo_root(self.egress_allowlist_file)
        self.coding_checkpoints_dir = _from_repo_root(self.coding_checkpoints_dir)
        self.coding_work_dir = _from_repo_root(self.coding_work_dir)
        return self

    @property
    def cost_known(self) -> bool:
        return self.openai_price_input_per_mtok is not None and self.openai_price_output_per_mtok is not None

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.devpilot_cors_origins.split(",") if o.strip()]

    @property
    def database_path(self) -> Path | None:
        """Filesystem path of the SQLite database, or None for in-memory."""
        raw = self.database_url.removeprefix("sqlite:///")
        if raw in ("", ":memory:"):
            return None
        return _from_repo_root(Path(raw))

    @property
    def resolved_database_url(self) -> str:
        path = self.database_path
        return "sqlite://" if path is None else f"sqlite:///{path.as_posix()}"

    def secret_values(self) -> list[str]:
        """Every configured secret value, for the redactor."""
        values: list[str] = []
        for name in type(self).model_fields:
            field_value = getattr(self, name)
            if isinstance(field_value, SecretStr):
                secret = field_value.get_secret_value()
                if secret:
                    values.append(secret)
        return values


def _from_repo_root(path: Path) -> Path:
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required values come from env
