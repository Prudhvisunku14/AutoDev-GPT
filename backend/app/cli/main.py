"""The `devpilot` command: build, doctor, setup."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.cli import credentials
from app.cli.paths import HOME_ENV, paths
from app.cli.userconfig import ConfigError, load

EXAMPLES = """\
examples:
  devpilot setup                                   one-time: key, tools, Docker runtime image
  devpilot doctor                                  check this computer; says how to fix anything missing
  devpilot build                                   asks what to build, then for your spending approval
  devpilot build "a python tool that converts csv files to json, with unit tests"
  devpilot build "..." --approve-spend 0.50        approve non-interactively (the figure must equal the limit)

A build spends your own OpenAI credit, never more than the limit you approve. Only Python command-line applications are built and
verified so far; requests for other kinds (React, websites, full-stack) are declined.
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devpilot", description="Describe an application; DevPilot plans it with GPT, writes it with OpenCode, tests it in Docker and saves a verified ZIP.",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter,
    )  # fmt: skip
    parser.add_argument("--home", type=Path, help=f"where DevPilot keeps its files (default: %%LOCALAPPDATA%%\\DevPilot, or {HOME_ENV})")
    sub = parser.add_subparsers(dest="command", metavar="command")

    build = sub.add_parser("build", help="build an application (asks for a description and your spending approval)", formatter_class=argparse.RawDescriptionHelpFormatter)
    build.add_argument("prompt", nargs="?", help="what to build; if omitted you are asked")
    build.add_argument("--stack", help="the technology stack (only python-cli is available)")
    build.add_argument("--max-usd", type=float, help="the most this build may spend, GPT and OpenCode together (default: your configured limit)")
    build.add_argument("--output", type=Path, help="where to save the project folder and ZIP (default: ./devpilot-output)")
    build.add_argument("--approve-spend", type=float, metavar="USD", help="approve the spending limit without typing it; must equal the limit exactly")

    doctor = sub.add_parser("doctor", help="check that this computer can build, and say how to fix anything that cannot")
    doctor.add_argument("--check-key", action="store_true", help="also ask OpenAI whether your key can see the model (one free request, no tokens)")

    setup = sub.add_parser("setup", help="one-time setup: your key, OpenCode, ripgrep and the audited Docker runtime image")
    setup.add_argument("--key-from-env", action="store_true", help="use the OPENAI_API_KEY environment variable instead of asking")
    setup.add_argument("--model", help="the OpenAI model to use")
    setup.add_argument("--input-price", type=float, help="your price per 1M input tokens, USD")
    setup.add_argument("--output-price", type=float, help="your price per 1M output tokens, USD")
    setup.add_argument("--rebuild", action="store_true", help="build and audit the runtime image again")
    setup.add_argument("--yes", action="store_true", help="do not ask before approving the image that passed its audit")
    setup.add_argument("--skip-downloads", action="store_true", help="only configure; do not download tools or build the image")

    sub.add_parser("forget-key", help="delete the stored OpenAI key")
    return parser


def _utf8_output() -> None:
    """The messages use characters such as — and …; a Windows console on a legacy code page would otherwise raise on them."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    where = paths(args.home)
    try:
        config = load(where.config)
    except ConfigError as exc:
        print(f"Settings problem: {exc}")
        return 2

    if args.command == "doctor":
        from app.cli.doctor import blockers, render, run_checks

        checks = run_checks(where, config, check_key=args.check_key)
        print(render(checks))
        return 1 if blockers(checks) else 0
    if args.command == "setup":
        from app.cli.setup import setup_command

        return setup_command(
            where, config, interactive=sys.stdin.isatty(), key_from_env=args.key_from_env, model=args.model, input_price=args.input_price,
            output_price=args.output_price, rebuild=args.rebuild, assume_yes=args.yes, skip_provision=args.skip_downloads,
        )  # fmt: skip
    if args.command == "forget-key":
        print("The stored key was deleted." if credentials.forget(where.credentials) else "No key was stored.")
        return 0
    from app.cli.build import build_command

    return build_command(
        where, config, prompt=args.prompt, stack=args.stack, max_usd=args.max_usd, output=args.output, approve_spend=args.approve_spend,
        interactive=sys.stdin.isatty(),
    )  # fmt: skip


if __name__ == "__main__":
    sys.exit(main())
