"""The user's own settings: model, prices, the per-build spending cap and where results go. Secrets are not here (see credentials)."""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_MODEL = "gpt-6.1-sol"  # the model the verified builds ran on; setup asks, and the user's own account decides
DEFAULT_PRICE_INPUT = 2.0  # USD per 1M tokens; setup asks the user to confirm them against their own pricing page
DEFAULT_PRICE_OUTPUT = 10.0
DEFAULT_CAP_USD = 0.50  # one build, GPT and OpenCode together: the limit the verified builds ran under
MAX_CAP_USD = 2.00  # the most `--max-usd` may ask for: a mistyped figure must not approve more


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class UserConfig:
    model: str | None = None
    price_input_per_mtok: float | None = None
    price_output_per_mtok: float | None = None
    run_cap_usd: float = DEFAULT_CAP_USD
    output_dir: str | None = None
    # Where the prices came from: "typed" (the person entered or passed them), "default" (they pressed Enter on DevPilot's built-in
    # figures, which are what the OpenCode release lists, not an OpenAI-published rate), or None (a file from before this was recorded).
    prices_source: str | None = None

    @property
    def prices_set(self) -> bool:
        return self.price_input_per_mtok is not None and self.price_output_per_mtok is not None

    @property
    def prices_note(self) -> str:
        """How much to trust the prices, for any line that shows them."""
        return {
            "typed": "entered by you, not checked by DevPilot",
            "default": "DevPilot's built-in defaults, NOT an OpenAI-published rate: check them",
        }.get(self.prices_source or "", "source not recorded: check them against your OpenAI pricing page")

    def with_(self, **changes: object) -> UserConfig:
        return replace(self, **changes)  # type: ignore[arg-type]


def _positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive number")
    return float(value)


def validate(config: UserConfig) -> UserConfig:
    if config.model is not None and not config.model.strip():
        raise ConfigError("model is empty")
    if config.model is not None and ("/" in config.model or any(c.isspace() for c in config.model)):
        raise ConfigError("model is the model's own name, such as gpt-6.1-sol: no provider prefix, no spaces")
    for name, value in (("price_input_per_mtok", config.price_input_per_mtok), ("price_output_per_mtok", config.price_output_per_mtok)):
        if value is not None:
            _positive(value, name)
    if config.prices_source not in (None, "typed", "default"):
        raise ConfigError('prices_source must be "typed" or "default"')
    if not 0 < config.run_cap_usd <= MAX_CAP_USD:
        raise ConfigError(f"run_cap_usd must be above 0 and at most {MAX_CAP_USD:.2f}")
    return config


def load(path: Path) -> UserConfig:
    if not path.is_file():
        return UserConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path} cannot be read: {exc}") from exc
    known = {"model", "price_input_per_mtok", "price_output_per_mtok", "run_cap_usd", "output_dir", "prices_source"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"{path} has unknown settings: {', '.join(unknown)}")
    return validate(UserConfig(
        model=data.get("model"), price_input_per_mtok=data.get("price_input_per_mtok"), price_output_per_mtok=data.get("price_output_per_mtok"),
        run_cap_usd=float(data.get("run_cap_usd", DEFAULT_CAP_USD)), output_dir=data.get("output_dir"),
        prices_source=data.get("prices_source"),
    ))  # fmt: skip


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(path: Path, config: UserConfig) -> None:
    validate(config)
    lines = ["# DevPilot user settings. No secrets belong here: the OpenAI key is stored encrypted, apart from this file."]
    if config.model is not None:
        lines.append(f"model = {_quote(config.model)}")
    if config.price_input_per_mtok is not None:
        lines.append(f"price_input_per_mtok = {config.price_input_per_mtok!r}")
    if config.price_output_per_mtok is not None:
        lines.append(f"price_output_per_mtok = {config.price_output_per_mtok!r}")
    if config.prices_source is not None:
        lines.append(f"prices_source = {_quote(config.prices_source)}")
    lines.append(f"run_cap_usd = {config.run_cap_usd!r}")
    if config.output_dir is not None:
        lines.append(f"output_dir = {_quote(config.output_dir)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
