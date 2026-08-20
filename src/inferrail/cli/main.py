"""The Inferrail CLI.

Five commands for v0.1: `serve` (run the gateway), `config check`
(validate inferrail.yaml plus referenced secrets without starting a
server), `report` (aggregate local economic receipts by a dimension),
`demo` (offline, zero-key walkthrough of the receipt/report pipeline), and
`try` (one real request through the same InferenceEngine, no config file
required). Additional commands (`routes`, `providers`, `doctor`, `stats`)
are plausible follow-ups but aren't implemented yet — see docs/PRODUCT.md.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from inferrail.cli.demo import run_demo
from inferrail.cli.report import run_report
from inferrail.cli.try_cmd import run_try
from inferrail.config.loader import load_config
from inferrail.config.quickstart import (
    QUICKSTART_MODEL,
    QUICKSTART_RECEIPTS_PATH,
    build_quickstart_config,
)
from inferrail.errors import ConfigurationError
from inferrail.providers.registry import build_providers


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inferrail",
        description="Inferrail: an OpenAI-compatible LLM gateway with cost attribution.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Start the Inferrail gateway.")
    serve.add_argument(
        "--config", default="inferrail.yaml", help="Path to inferrail.yaml (default: %(default)s)"
    )
    serve.add_argument("--host", default=None, help="Override server.host from config")
    serve.add_argument("--port", type=int, default=None, help="Override server.port from config")
    serve.add_argument(
        "--quickstart",
        action="store_true",
        help=(
            "Skip inferrail.yaml entirely and serve with in-memory quickstart "
            f"defaults (OpenAI/{QUICKSTART_MODEL}, receipts at {QUICKSTART_RECEIPTS_PATH}). "
            "Requires OPENAI_API_KEY."
        ),
    )

    subparsers.add_parser(
        "demo", help="Offline, zero-key walkthrough of the receipt/report pipeline."
    )

    try_parser = subparsers.add_parser(
        "try",
        help="Send one real request through Inferrail's InferenceEngine (needs OPENAI_API_KEY).",
    )
    try_parser.add_argument("prompt", help="The user message to send.")
    try_parser.add_argument(
        "--customer", default=None, help="Shorthand for -a customer=<value>."
    )
    try_parser.add_argument(
        "--workflow", default=None, help="Shorthand for -a workflow=<value>."
    )
    try_parser.add_argument(
        "-a",
        "--attribute",
        dest="attributes",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Attach generic business attribution, e.g. -a environment=prod. Repeatable.",
    )
    try_parser.add_argument(
        "--model",
        default=QUICKSTART_MODEL,
        help="Model to use via the quickstart OpenAI route (default: %(default)s).",
    )

    config_parser = subparsers.add_parser("config", help="Configuration utilities.")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    check = config_sub.add_parser(
        "check", help="Validate inferrail.yaml and confirm referenced secrets are present."
    )
    check.add_argument(
        "--config", default="inferrail.yaml", help="Path to inferrail.yaml (default: %(default)s)"
    )

    report = subparsers.add_parser(
        "report", help="Aggregate local economic receipts by a dimension."
    )
    report.add_argument(
        "--by",
        required=True,
        help=(
            "Dimension to group by: 'provider', 'model', 'route', or any "
            "attribution attribute name (e.g. 'customer', 'workflow')."
        ),
    )
    report.add_argument(
        "--receipts",
        default=None,
        help="Path to the receipts JSONL file. Defaults to receipts.path from --config.",
    )
    report.add_argument(
        "--config",
        default=None,
        help=(
            "Path to inferrail.yaml (default: inferrail.yaml). If omitted and no "
            "inferrail.yaml exists, falls back to the quickstart receipts path "
            "instead of erroring. An explicitly-given --config that doesn't exist "
            "always errors, regardless of this fallback."
        ),
    )

    return parser


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from inferrail.gateway.app import create_app

    try:
        if args.quickstart:
            print("No inferrail.yaml used — running with quickstart defaults:")
            print("  provider: OpenAI")
            print(f"  models:   passthrough (any OpenAI model id works, e.g. {QUICKSTART_MODEL})")
            print(f"  receipts: {QUICKSTART_RECEIPTS_PATH}")
            print()
            print("To persist/customize configuration: cp inferrail.example.yaml inferrail.yaml")
            print()
            config = build_quickstart_config()
        else:
            config = load_config(args.config)
        app = create_app(config)
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    host = args.host or config.server.host
    port = args.port or config.server.port
    uvicorn.run(app, host=host, port=port)
    return 0


def _cmd_config_check(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        build_providers(config)
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"{args.config}: OK")
    print(f"  providers:        {', '.join(sorted(config.providers))}")
    print(f"  routes:           {', '.join(sorted(config.routes))}")
    if config.default_provider is not None:
        print(f"  default_provider: {config.default_provider} (unmatched models pass through)")
    print(f"  telemetry:        {config.telemetry.sink}")
    print(f"  receipts:         {config.receipts.sink}")
    print(f"  server:           {config.server.host}:{config.server.port}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    if args.receipts is not None:
        receipts_path = Path(args.receipts)
    elif args.config is None and not Path("inferrail.yaml").exists():
        # The user did not explicitly pass --config, and there's no default
        # inferrail.yaml to read receipts.path from — fall back to the same
        # default `inferrail try`/`inferrail serve --quickstart` write to,
        # so `inferrail report --by customer` works immediately after the
        # quickstart path with no extra flags. An *explicitly* given
        # --config that's missing/invalid (below) always errors loudly
        # instead — silently falling back there would mask a typo.
        receipts_path = Path(QUICKSTART_RECEIPTS_PATH)
    else:
        try:
            config = load_config(args.config or "inferrail.yaml")
        except ConfigurationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        receipts_path = Path(config.receipts.path)

    return run_report(receipts_path, args.by)


def _cmd_demo(args: argparse.Namespace) -> int:
    del args  # no arguments
    return run_demo()


def _cmd_try(args: argparse.Namespace) -> int:
    return run_try(
        args.prompt,
        customer=args.customer,
        workflow=args.workflow,
        attribute_args=args.attributes,
        model=args.model,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # find_dotenv(usecwd=True): without it, python-dotenv locates .env
    # relative to this installed module's own file path, not the directory
    # the user is actually running `inferrail` from — which only happens to
    # work today for an editable install invoked from the repo root, and
    # silently fails to find `.env` anywhere else (a different cwd, a
    # non-editable install). The failure mode is a confusing "environment
    # variable ... missing or empty" error even though `.env` is sitting
    # right there.
    load_dotenv(find_dotenv(usecwd=True))

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "config" and args.config_command == "check":
        return _cmd_config_check(args)
    if args.command == "report":
        return _cmd_report(args)
    if args.command == "demo":
        return _cmd_demo(args)
    if args.command == "try":
        return _cmd_try(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
