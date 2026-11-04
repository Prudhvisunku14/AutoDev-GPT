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
  devpilot inspect https://github.com/<owner>/<repo>     what a repository is (framework, pages, assets); free
  devpilot verify-site https://github.com/<owner>/<repo>  build and check an existing website in Docker; free
  devpilot setup --web                             one-time: build and self-test the website verifier
  devpilot setup --github-token                    store a GitHub token for private repositories (typed hidden)

A build spends your own OpenAI credit, never more than the limit you approve. Builds are accepted only for stacks DevPilot can
verify: Python command-line applications, and static websites (HTML, CSS, JavaScript) once `setup --web` has self-tested the
browser verifier. Either can start from your GitHub repository. A static website passes only if independent browser checks,
written from your request before any code exists, pass in a real browser. React, FastAPI and full-stack builds are declined.
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
    build.add_argument("--stack", help="the technology stack; normally chosen from the request or the repository")
    build.add_argument("--branch", help="with a GitHub repository in the request: the branch or tag to start from")
    build.add_argument("--commit", help="with a GitHub repository in the request: the exact commit SHA to start from")
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

    setup.add_argument("--web", action="store_true", help="build and self-test the website verifier (Docker; downloads about 2 GB once)")
    setup.add_argument("--github-token", action="store_true", help="store a GitHub token for reading private repositories (typed hidden)")

    sub.add_parser("forget-key", help="delete the stored OpenAI key")
    sub.add_parser("forget-github-token", help="delete the stored GitHub token")

    inspect = sub.add_parser("inspect", help="describe a GitHub repository: framework, stack, pages, components, assets (free)")
    inspect.add_argument("url", help="https://github.com/<owner>/<repository>")
    inspect.add_argument("--branch", help="a branch or tag")
    inspect.add_argument("--commit", help="an exact commit SHA")

    verify = sub.add_parser("verify-site", help="install, build and check an existing website in Docker with a real browser (free)")
    verify.add_argument("target", help="a folder, or https://github.com/<owner>/<repository>")
    verify.add_argument("--branch", help="a branch or tag (GitHub only)")
    verify.add_argument("--commit", help="an exact commit SHA (GitHub only)")
    verify.add_argument("--component", help="the folder holding the site, when the project has several")
    verify.add_argument("--output", type=Path, help="where to save the report (default: ./devpilot-output)")
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
    ref = getattr(args, "commit", None) or getattr(args, "branch", None)
    if getattr(args, "commit", None) and getattr(args, "branch", None):
        print("Give --branch or --commit, not both.")
        return 2
    if args.command == "setup" and (args.web or args.github_token):
        from app.cli.setup import setup_extras

        return setup_extras(where, web_runtime=args.web, github=args.github_token, rebuild=args.rebuild, interactive=sys.stdin.isatty())
    if args.command == "setup":
        from app.cli.setup import setup_command

        return setup_command(
            where, config, interactive=sys.stdin.isatty(), key_from_env=args.key_from_env, model=args.model, input_price=args.input_price,
            output_price=args.output_price, rebuild=args.rebuild, assume_yes=args.yes, skip_provision=args.skip_downloads,
        )  # fmt: skip
    if args.command == "forget-key":
        print("The stored key was deleted." if credentials.forget(where.credentials) else "No key was stored.")
        return 0
    if args.command == "forget-github-token":
        from app.cli import github_token

        print("The stored GitHub token was deleted." if github_token.forget(where.github_credentials) else "No GitHub token was stored.")
        return 0
    if args.command == "inspect":
        from app.cli.inspect_cmd import inspect_command

        return inspect_command(where, args.url, ref=ref)
    if args.command == "verify-site":
        from app.cli.verify_site import verify_site_command

        return verify_site_command(where, args.target, ref=ref, component=args.component, output=args.output)
    from app.cli.build import build_command

    return build_command(
        where, config, prompt=args.prompt, stack=args.stack, max_usd=args.max_usd, output=args.output, approve_spend=args.approve_spend,
        interactive=sys.stdin.isatty(), ref=ref,
    )  # fmt: skip


if __name__ == "__main__":
    sys.exit(main())
