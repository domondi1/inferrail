"""The Inferrail CLI.

Only two commands for v0.1: `serve` (run the gateway) and `config check`
(validate inferrail.yaml plus referenced secrets without starting a
server). Additional commands (`routes`, `providers`, `doctor`, `stats`) are
plausible follow-ups but aren't implemented yet — see docs/PRODUCT.md.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

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
    print(f"  server:    {config.server.host}:{config.server.port}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv()

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "config" and args.config_command == "check":
        return _cmd_config_check(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
