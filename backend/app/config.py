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
    # USD per 1M tokens. Unset means "cost unknown"; the API refuses to start runs without an enforceable USD cap.
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
    # A start must then carry a spending approval (POST /runs/{id}/start with approved_max_usd equal to RUN_BUDGET_USD).
    # The dashboard always sends one. The terminal scripts start runs behind their own flags and typed confirmations;
    # scripts/live_server.py, which serves the dashboard for real builds, sets this.
    require_spend_approval: bool = False

    # --- Earlier terminal builds (scripts/live_demo.py, scripts/live_build.py) ------
    # Shown read-only, from each run folder's summary.json and workspace. Their databases are never opened.
    history_dir: Path | None = None  # off unless set: a folder of earlier terminal builds, which only the developer's machine has

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

    # --- Adaptive tool selection (app/selection/) ------------------------------
    # `fixed` keeps the first available provider in preference order, for comparison or as a way back.
    selection_policy: Literal["adaptive", "fixed"] = "adaptive"
    # Initial weights, not tuned. They must add up to 1.
    selection_weight_capability: float = Field(default=0.30, ge=0, le=1)
    selection_weight_reliability: float = Field(default=0.25, ge=0, le=1)
    selection_weight_quality: float = Field(default=0.15, ge=0, le=1)
    selection_weight_latency: float = Field(default=0.10, ge=0, le=1)
    selection_weight_cost: float = Field(default=0.10, ge=0, le=1)
    selection_weight_resource: float = Field(default=0.10, ge=0, le=1)
    # Prior for (successes + alpha) / (attempts + alpha + beta): as if four attempts had split evenly.
    selection_reliability_alpha: float = Field(default=2.0, gt=0)
    selection_reliability_beta: float = Field(default=2.0, gt=0)
    # A value equal to its reference scores 0.5.
    selection_latency_reference_seconds: float = Field(default=120.0, gt=0)
    selection_cost_reference_usd: float = Field(default=0.25, gt=0)
    selection_memory_reference_mb: float = Field(default=2_048.0, gt=0)

    # --- Failure recovery (app/recovery/) --------------------------------------
    # `bounded` keeps the fixed retry and repair rules, for comparison or as a way back.
    recovery_policy: Literal["intelligent", "bounded"] = "intelligent"
    # The same failure this many times in a row: the action taken after it is not working.
    recovery_repeat_limit: int = Field(default=2, ge=2, le=10)
    # Below this rate, over at least this many verified repairs, that repair is not tried.
    recovery_min_repair_rate: float = Field(default=0.25, ge=0, le=1)
    recovery_min_repair_samples: int = Field(default=5, ge=1)
    # The only setting recovery may change: how long one task's coding attempt may run. It never
    # touches a test timeout, a memory limit, the network or a spending limit. Unset, it is
    # OPENCODE_TIMEOUT_SECONDS, so no attempt is given longer until this is raised on purpose.
    recovery_max_attempt_timeout_seconds: float | None = Field(default=None, gt=0, le=3600)

    # --- Public repository reuse (app/repos/) ------------------------------
    # Every search and import also needs a person's approval and the egress allowlist's `github` entries.
    repository_source: Literal["off", "github"] = "off"  # github: the GitHub client, public data only, no token
    repository_search_limit: int = Field(default=8, ge=1, le=20)
    repository_reuse_threshold: float = Field(default=0.55, ge=0, le=1)  # the least score a repository needs to be started from
    repository_max_archive_mb: float = Field(default=20.0, gt=0, le=200)
    repository_max_files: int = Field(default=2_000, ge=1, le=20_000)

    # --- n8n workflows (ARCHITECTURE §8.1) ----------------------------------
    # Off by default. On, GPT may trigger only the workflows listed in N8N_WORKFLOWS_FILE, each call signed with
    # N8N_WEBHOOK_SECRET and waiting for a person's approval unless N8N_AUTO_APPROVE=true. n8n answers asynchronously
    # at POST /integrations/n8n/callback, signed the same way. The base URL must also be in the egress allowlist's n8n entry.
    n8n_enabled: bool = False
    n8n_base_url: str = "http://127.0.0.1:5678"
    n8n_workflows_file: Path = Path("config/n8n_workflows.yaml")
    # Reviewed facts about each development tool (app/toolcatalog). Readiness is computed, never read from this file.
    tool_catalog_file: Path = Path("config/tool_catalog.yaml")
    n8n_auto_approve: bool = False
    n8n_callback_url: str | None = None  # where n8n should post results, as n8n sees DevPilot; None: no callback is offered
    n8n_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    n8n_max_response_bytes: int = Field(default=64_000, ge=1_024, le=1_000_000)

    # --- Google Drive and the RAG knowledge base (Milestone 6) --------------
    # off: no Drive tools. google: the Drive v3 client and the OAuth flow (app/drive/oauth.py), which fails closed
    # until cryptography is installed and DEVPILOT_TOKEN_KEY is set, and has never run against Google. Tests and the
    # offline demonstration pass a fixture runtime to create_app; no setting selects fixtures or the embedding test double.
    drive_source: Literal["off", "google"] = "off"
    # The person's GitHub connection (app/repos/github_oauth.py): an OAuth App they register at github.com/settings/developers,
    # with this callback. The token is stored encrypted with DEVPILOT_TOKEN_KEY under CREDENTIALS_DIR; nobody pastes one.
    github_oauth_client_id: str | None = None  # public identifier; its secret is github_oauth_client_secret
    github_oauth_redirect_uri: str = "http://127.0.0.1:8000/oauth/github/callback"
    github_oauth_scopes: str = "repo"  # read private repositories the person chooses, and open approved pull requests
    credentials_dir: Path = Path("data/credentials")
    dashboard_url: str = "http://localhost:5173"  # where the launcher's one-time sign-in link points
    google_oauth_client_id: str | None = None  # public identifier; its secret is google_oauth_client_secret
    google_oauth_redirect_uri: str = "http://127.0.0.1:8000/oauth/google/callback"
    drive_max_files: int = Field(default=200, ge=1, le=5_000)  # discovered, and ingested in one call
    drive_max_file_mb: float = Field(default=10.0, gt=0, le=100)
    drive_max_total_mb: float = Field(default=100.0, gt=0, le=2_000)  # read in one ingestion call
    drive_max_depth: int = Field(default=3, ge=0, le=10)  # folders opened below an approved one
    drive_page_size: int = Field(default=100, ge=1, le=1_000)
    rag_max_embedding_tokens: int = Field(default=2_000_000, gt=0)  # estimated, before anything is embedded
    # Real semantic retrieval (app/rag/openai_embedder.py): off by default. "openai" embeds with OPENAI_EMBEDDING_MODEL, bounded by
    # RAG_EMBEDDING_MAX_USD at OPENAI_EMBEDDING_PRICE_PER_MTOK (both required), and keeps vectors in SQLite under RAG_INDEX_DIR.
    rag_embedder: Literal["none", "openai"] = "none"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimensions: int = Field(default=512, ge=64, le=3072)
    openai_embedding_price_per_mtok: float | None = Field(default=None, gt=0)
    rag_embedding_max_usd: float = Field(default=0.10, gt=0, le=5)
    rag_index_dir: Path = Path("data/rag")

    # --- Credentials used by later phases ---------------------------------
    # Declared here only so the redactor knows their values. Phase 1 never uses them.
    openai_api_key: SecretStr | None = None
    opencode_server_password: SecretStr | None = None
    n8n_api_key: SecretStr | None = None
    n8n_webhook_secret: SecretStr | None = None
    github_token: SecretStr | None = None
    google_oauth_client_secret: SecretStr | None = None
    devpilot_token_key: SecretStr | None = None
    github_oauth_client_secret: SecretStr | None = None

    @field_validator(
        "openai_api_key",
        "opencode_server_password",
        "n8n_api_key",
        "n8n_webhook_secret",
        "github_token",
        "google_oauth_client_secret",
        "devpilot_token_key",
        "github_oauth_client_secret",
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
        "recovery_max_attempt_timeout_seconds",
        "history_dir",
        "github_oauth_client_id",
        "openai_embedding_price_per_mtok",
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
        self.n8n_workflows_file = _from_repo_root(self.n8n_workflows_file)
        self.tool_catalog_file = _from_repo_root(self.tool_catalog_file)
        self.credentials_dir = _from_repo_root(self.credentials_dir)
        self.rag_index_dir = _from_repo_root(self.rag_index_dir)
        self.coding_checkpoints_dir = _from_repo_root(self.coding_checkpoints_dir)
        self.coding_work_dir = _from_repo_root(self.coding_work_dir)
        return self

    @model_validator(mode="after")
    def _selection_weights_add_up(self) -> Settings:
        total = (
            self.selection_weight_capability + self.selection_weight_reliability + self.selection_weight_quality
            + self.selection_weight_latency + self.selection_weight_cost + self.selection_weight_resource
        )  # fmt: skip
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"the SELECTION_WEIGHT_* values must add up to 1, not {total:.6f}")
        return self

    @property
    def cost_known(self) -> bool:
        return self.openai_price_input_per_mtok is not None and self.openai_price_output_per_mtok is not None

    @property
    def recovery_attempt_timeout_ceiling(self) -> float:
        """The longest one coding attempt may be given. Unset, the normal limit: recovery never extends one."""
        if self.recovery_max_attempt_timeout_seconds is None:
            return self.opencode_timeout_seconds
        return self.recovery_max_attempt_timeout_seconds

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
