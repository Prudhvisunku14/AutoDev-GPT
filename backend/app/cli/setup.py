"""`devpilot setup`: everything a new installation needs, each step verified, none of it silent about what it does.

It collects the user's own OpenAI key (hidden input, stored encrypted for this Windows user), asks for the model and the user's own
prices, downloads and verifies OpenCode and ripgrep (both pinned by hash), builds the runtime image on this machine, audits its
contents offline, and only then records it as approved here. It never prints the key, and it makes no OpenAI request.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable

from app.cli import credentials, runtime
from app.cli.build import IO, ConsoleIO, secret_prompt
from app.cli.doctor import blockers, render, run_checks
from app.cli.paths import Paths
from app.cli.userconfig import DEFAULT_MODEL, DEFAULT_PRICE_INPUT, DEFAULT_PRICE_OUTPUT, ConfigError, UserConfig, save, validate


def _ask_number(io: IO, prompt: str, default: float) -> tuple[float, bool]:
    """The number, and whether the person typed it (False: they pressed Enter on the default)."""
    while True:
        answer = io.ask(f"{prompt} [{default:g}]: ").strip()
        try:
            return (float(answer), True) if answer else (default, False)
        except ValueError:
            io.say("Enter a number.")


def configure(paths: Paths, config: UserConfig, io: IO, *, key_from_env: bool, ask_secret: Callable[[str], str], interactive: bool,
              model: str | None, input_price: float | None, output_price: float | None) -> UserConfig:  # fmt: skip
    """The key, the model and the prices. Anything already set is kept unless a flag changes it."""
    where = credentials.source(paths.credentials)
    if key_from_env and not os.environ.get(credentials.ENV_VAR, "").strip():
        raise runtime.ProvisionError(f"--key-from-env was given, but the {credentials.ENV_VAR} environment variable is not set")
    if where is not None:
        io.say(f"\nUsing the OpenAI key from {where}." + (" It is not stored by DevPilot." if "environment" in where else ""))
    else:
        if not interactive:
            raise runtime.ProvisionError(f"no OpenAI key: set the {credentials.ENV_VAR} environment variable, or run setup in a terminal")
        io.say("\nYour OpenAI API key is needed. It is typed hidden, stored encrypted for your Windows account only (never in a file you could")
        io.say("commit), and sent only to OpenAI. Get one at https://platform.openai.com/api-keys")
        key = ask_secret("OpenAI API key (input is hidden): ").strip()
        try:
            credentials.store_key(paths.credentials, key)
        except credentials.CredentialError as exc:
            raise runtime.ProvisionError(str(exc)) from exc
        io.say("  key stored.")

    chosen_model = model or config.model
    if chosen_model is None and interactive:
        chosen_model = io.ask(f"\nOpenAI model to use [{DEFAULT_MODEL}]: ").strip() or DEFAULT_MODEL
    chosen_in = input_price if input_price is not None else config.price_input_per_mtok
    chosen_out = output_price if output_price is not None else config.price_output_per_mtok
    source = config.prices_source
    if input_price is not None or output_price is not None:
        source = "typed"  # passed on the command line: the person's own figures
    if (chosen_in is None or chosen_out is None) and interactive:
        io.say("\nPrices, in US dollars per 1 million tokens, from your own OpenAI pricing page. DevPilot uses them to cap your spending:")
        io.say("a build cannot start without them. Wrong (too low) prices make the cap too loose, so check them.")
        io.say(f"The figures in brackets (${DEFAULT_PRICE_INPUT:g} and ${DEFAULT_PRICE_OUTPUT:g}) are DevPilot's own defaults, taken from the OpenCode release.")
        io.say("They are NOT an official OpenAI rate and may be out of date: type your own, or press Enter to keep them (they are then recorded as defaults).")
        chosen_in, typed_in = _ask_number(io, "  Input price", DEFAULT_PRICE_INPUT)
        chosen_out, typed_out = _ask_number(io, "  Output price", DEFAULT_PRICE_OUTPUT)
        source = "typed" if typed_in and typed_out else "default"
    updated = validate(config.with_(model=chosen_model, price_input_per_mtok=chosen_in, price_output_per_mtok=chosen_out, prices_source=source))
    save(paths.config, updated)
    return updated


def provision(paths: Paths, io: IO, *, rebuild: bool, assume_yes: bool, run: runtime.Runner = subprocess.run) -> None:
    io.say("\nDownloading and verifying ripgrep (pinned SHA-256)…")
    runtime.fetch_ripgrep(paths)
    io.say("  ok.")
    io.say("Downloading and verifying OpenCode (pinned SHA-512; this is ~80 MB the first time)…")
    runtime.fetch_opencode(paths)
    io.say("  ok.")

    probe = runtime.docker(["version", "--format", "{{.Server.Version}}"], run=run, timeout=30)
    if probe.returncode != 0:
        raise runtime.ProvisionError("Docker is installed but not running: start Docker Desktop, wait until it says it is running, then run setup again")
    if runtime.docker(["image", "inspect", "python:3.12-alpine"], run=run, timeout=30).returncode != 0:
        io.say("Pulling the sandbox base image python:3.12-alpine…")
        pulled = runtime.docker(["pull", "python:3.12-alpine"], run=run, timeout=600)
        if pulled.returncode != 0:
            raise runtime.ProvisionError(f"could not pull python:3.12-alpine: {pulled.stderr.strip()[-300:]}")

    current, _ = runtime.load_approval(paths, run=run)
    if current is not None and not rebuild:
        io.say(f"The runtime image is already audited and approved ({current[:19]}…). Use --rebuild to build and audit it again.")
        return
    io.say(f"Building the runtime image {runtime.RUNTIME_TAG} (needs the internet once; the containers it starts never have any)…")
    runtime.build_image(paths, run=run)
    io.say("Auditing the image's contents offline (user, layers, the one added file, runtime libraries)…")
    io.say("  This checks: it runs as a non-root user; its last layer holds only the hash-pinned ripgrep; it has the C++ libraries OpenCode needs.")
    if not assume_yes and io.ask("Approve the image that passes this audit for DevPilot on this computer? [Y/n]: ").strip().lower() not in ("", "y", "yes"):
        raise runtime.ProvisionError("the image was not approved, so DevPilot will not run it")
    approval = runtime.audit_and_approve(paths, run=run)
    runtime.load_approval(paths, run=run)
    io.say(f"  audited and approved: {approval.image_id[:19]}…")


def setup_command(
    paths: Paths,
    config: UserConfig,
    *,
    io: IO | None = None,
    interactive: bool = True,
    key_from_env: bool = False,
    model: str | None = None,
    input_price: float | None = None,
    output_price: float | None = None,
    rebuild: bool = False,
    assume_yes: bool = False,
    skip_provision: bool = False,
    ask_secret: Callable[[str], str] = secret_prompt,
    run: runtime.Runner = subprocess.run,
) -> int:
    io = io or ConsoleIO()
    paths.ensure()
    io.say(f"DevPilot's files for this user: {paths.home}")
    try:
        config = configure(paths, config, io, key_from_env=key_from_env, ask_secret=ask_secret, interactive=interactive, model=model, input_price=input_price, output_price=output_price)
        if not skip_provision:
            provision(paths, io, rebuild=rebuild, assume_yes=assume_yes, run=run)
    except (runtime.ProvisionError, ConfigError) as exc:
        io.say(f"\nSetup stopped: {exc}")
        return 1
    io.say("\nChecking everything…")
    checks = run_checks(paths, config)
    io.say(render(checks))
    return 1 if blockers(checks) else 0
