"""The Inferrail CLI.

Three commands for v0.1: `serve` (run the gateway), `config check`
(validate inferrail.yaml plus referenced secrets without starting a
server), and `report` (aggregate local economic receipts by a dimension).
Additional commands (`routes`, `providers`, `doctor`, `stats`) are
plausible follow-ups but aren't implemented yet — see docs/PRODUCT.md.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from inferrail.cli.report import run_report
from inferrail.config.loader import load_config
from inferrail.errors import ConfigurationError
from inferrail.providers.registry import build_providers


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inferrail", description="Inferrail: an open inference control plane."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Start the Inferrail gateway.")
    serve.add_argument(
        "--config", default="inferrail.yaml", help="Path to inferrail.yaml (default: %(default)s)"
    )
    serve.add_argument("--host", default=None, help="Override server.host from config")
    serve.add_argument("--port", type=int, default=None, help="Override server.port from config")

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
        "--config", default="inferrail.yaml", help="Path to inferrail.yaml (default: %(default)s)"
    )

    return parser


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from inferrail.gateway.app import create_app

    try:
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
    print(f"  providers: {', '.join(sorted(config.providers))}")
    print(f"  routes:    {', '.join(sorted(config.routes))}")
    print(f"  telemetry: {config.telemetry.sink}")
    print(f"  receipts:  {config.receipts.sink}")
    print(f"  server:    {config.server.host}:{config.server.port}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    if args.receipts is not None:
        receipts_path = Path(args.receipts)
    else:
        try:
            config = load_config(args.config)
        except ConfigurationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        receipts_path = Path(config.receipts.path)

    return run_report(receipts_path, args.by)


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

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
