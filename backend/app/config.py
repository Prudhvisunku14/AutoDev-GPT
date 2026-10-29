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
        return self

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
