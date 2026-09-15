"""The Inferrail CLI.

Seven commands for v0.1: `serve` (run the gateway), `config check`
(validate inferrail.yaml plus referenced secrets without starting a
server), `report` (aggregate local economic receipts by a dimension),
`transaction` (show one task's aggregated economic transaction — see
docs/adr/0008-task-transactions.md), `demo` (offline, zero-key walkthrough
of the receipt/report pipeline), `work` (record outcomes and derive work
economics), and `try` (one real request through the
same InferenceEngine, no config file required). Additional commands
(`routes`, `providers`, `doctor`, `stats`) are plausible follow-ups but
aren't implemented yet — see docs/PRODUCT.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from inferrail.appdata import ensure_app_data_dir
from inferrail.cli.ap import (
    run_ap_batch,
    run_ap_demo,
    run_ap_outcome,
    run_ap_reap,
    run_ap_report,
)
from inferrail.cli.budget import run_budget_list, run_budget_rm, run_budget_set
from inferrail.cli.demo import run_demo
from inferrail.cli.doctor import run_doctor
from inferrail.cli.pricing import run_pricing_update
from inferrail.cli.receipts_io import run_receipts_export, run_receipts_import
from inferrail.cli.report import run_report
from inferrail.cli.transaction import run_transaction
from inferrail.cli.try_cmd import run_try
from inferrail.cli.work import DEFAULT_OUTCOMES_PATH, run_work, run_work_outcome
from inferrail.config.loader import load_config
from inferrail.config.models import BudgetsConfig, InferrailConfig, ReceiptsConfig
from inferrail.config.quickstart import (
    QUICKSTART_MODEL,
    QUICKSTART_RECEIPTS_PATH,
    build_quickstart_config,
)
from inferrail.errors import ConfigurationError
from inferrail.providers.registry import build_anthropic_providers, build_providers


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
    serve.add_argument(
        "--app-mode",
        action="store_true",
        help=(
            "Load providers/routes from --config as normal, but relocate "
            "receipts and budgets under the OS app-data directory (forcing "
            "receipts.sink: sqlite and budgets.enabled: true regardless of "
            "what inferrail.yaml says) and mount the local control API "
            "(/v1/local/*) guarded by a per-install token. See "
            "docs/adr/0016-local-control-api.md. Not combinable with "
            "--quickstart."
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
    try_parser.add_argument("--customer", default=None, help="Shorthand for -a customer=<value>.")
    try_parser.add_argument("--workflow", default=None, help="Shorthand for -a workflow=<value>.")
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
        required=False,
        help=(
            "Dimension to group by: 'provider', 'model', 'route', or any "
            "attribution attribute name (e.g. 'customer', 'workflow'). "
            "Omit for an all-up spend summary."
        ),
    )
    report.add_argument(
        "--receipts",
        default=None,
        help="Path to the receipts file (JSONL or SQLite). Default: receipts.path from --config.",
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

    transaction = subparsers.add_parser(
        "transaction",
        help="Show one task's aggregated economic transaction (see docs/adr/0008).",
    )
    transaction.add_argument(
        "task_id", help="The task id to look up (matched against an attribution attribute)."
    )
    transaction.add_argument(
        "--attribute-name",
        default="task_id",
        help=(
            "Attribution attribute name that identifies a task, e.g. set via "
            "'X-Inferrail-Attribute-Task-Id: bug_9281' (default: %(default)s)."
        ),
    )
    transaction.add_argument(
        "--receipts",
        default=None,
        help="Path to the receipts file (JSONL or SQLite). Default: receipts.path from --config.",
    )
    transaction.add_argument(
        "--config",
        default=None,
        help=(
            "Path to inferrail.yaml (default: inferrail.yaml). Same fallback "
            "behavior as 'report --config'."
        ),
    )
    transaction.add_argument(
        "--json",
        action="store_true",
        help="Print the raw TaskTransaction JSON instead of a formatted summary.",
    )

    work = subparsers.add_parser(
        "work", help="Record customer outcomes and inspect derived work-level inference economics."
    )
    work.add_argument(
        "work_or_action",
        nargs="?",
        help="Work id to inspect, or 'outcome' followed by a work id to record an outcome.",
    )
    work.add_argument("outcome_work_id", nargs="?", help=argparse.SUPPRESS)
    work.add_argument(
        "--status", help="Customer-declared outcome status (used with 'work outcome')."
    )
    work.add_argument(
        "--all", action="store_true", help="Show all work found in receipt or outcome evidence."
    )
    work.add_argument(
        "--receipts",
        default=None,
        help="Path to the receipts file (JSONL or SQLite; defaults to --config / quickstart path).",
    )
    work.add_argument(
        "--outcomes", default=str(DEFAULT_OUTCOMES_PATH), help="Path to append-only outcome JSONL."
    )
    work.add_argument(
        "--config", default=None, help="Path to inferrail.yaml for the receipts path."
    )
    work.add_argument("--json", action="store_true", help="Print derived work data as JSON.")

    receipts = subparsers.add_parser(
        "receipts", help="Move receipts between the JSONL and SQLite sinks."
    )
    receipts_sub = receipts.add_subparsers(dest="receipts_command", required=True)

    receipts_import = receipts_sub.add_parser(
        "import", help="Import a receipts JSONL file into a SQLite receipts store."
    )
    receipts_import.add_argument("--jsonl", required=True, help="Path to the source JSONL file.")
    receipts_import.add_argument(
        "--db", required=True, help="Path to the destination SQLite receipts store."
    )

    receipts_export = receipts_sub.add_parser(
        "export", help="Export a SQLite receipts store to a JSONL file."
    )
    receipts_export.add_argument(
        "--db", required=True, help="Path to the source SQLite receipts store."
    )
    receipts_export.add_argument(
        "--jsonl", required=True, help="Path to the destination JSONL file."
    )

    ap = subparsers.add_parser(
        "ap", help="AP invoice-exception recovery: decide, execute, and record retry vs. review."
    )
    ap_sub = ap.add_subparsers(dest="ap_command", required=True)

    ap_sub.add_parser(
        "demo", help="Fixture-based, zero-key walkthrough of the AP recovery engine."
    )

    ap_report = ap_sub.add_parser("report", help="Print the auditable report for a store.")
    ap_report.add_argument("--db", required=True, help="Path to the AP RecoveryStore sqlite3 file.")
    ap_report.add_argument("--json", action="store_true", help="Print the report as JSON.")

    ap_outcome = ap_sub.add_parser(
        "outcome", help="Record a real human-review outcome for a work_id."
    )
    ap_outcome.add_argument("--db", required=True, help="Path to the AP RecoveryStore file.")
    ap_outcome.add_argument("work_id")
    ap_outcome.add_argument(
        "--outcome", required=True, choices=["accepted", "corrected", "rejected", "escalated"]
    )
    ap_outcome.add_argument("--correction-delta-usd", default=None)
    ap_outcome.add_argument("--review-cost-usd", default=None)
    ap_outcome.add_argument("--source", default="cli")

    ap_reap = ap_sub.add_parser(
        "reap",
        help=(
            "Operator recovery: move any decision whose retry lease has expired to "
            "awaiting_human_review, without re-invoking the retry adapter."
        ),
    )
    ap_reap.add_argument("--db", required=True, help="Path to the AP RecoveryStore sqlite3 file.")
    ap_reap.add_argument("--json", action="store_true", help="Print the reaped work_ids as JSON.")

    ap_batch = ap_sub.add_parser(
        "batch",
        help=(
            "Historical/shadow-mode analysis over an already-exported vendor dataset "
            "(never executes anything)."
        ),
    )
    ap_batch.add_argument("--attempts", required=True, help="Path to a JSON array of attempt rows.")
    ap_batch.add_argument("--reviews", required=True, help="Path to a JSON array of review rows.")
    ap_batch.add_argument("--out", required=True, help="Path to write the auditable report (JSON).")
    ap_batch.add_argument("--vendor-confidence-threshold", required=True, type=float)
    ap_batch.add_argument("--candidate-human-review-threshold", required=True, type=float)
    ap_batch.add_argument("--candidate-retry-floor", required=True, type=float)
    ap_batch.add_argument("--candidate-max-prior-attempts", type=int, default=1)

    budget = subparsers.add_parser(
        "budget",
        help="Manage spend budgets (see docs/adr/0015-budget-enforcement.md).",
    )
    budget_sub = budget.add_subparsers(dest="budget_command", required=True)

    budget_set = budget_sub.add_parser(
        "set", help="Create or update a budget (upsert, keyed on scope/scope-value/window)."
    )
    budget_set.add_argument(
        "--scope", required=True, choices=["global", "project", "work_id"]
    )
    budget_set.add_argument(
        "--scope-value",
        default=None,
        help="Required for --scope project/work_id; must be omitted for --scope global.",
    )
    budget_set.add_argument(
        "--window", required=True, choices=["per_work", "daily", "monthly"]
    )
    budget_set.add_argument("--mode", required=True, choices=["warn", "block"])
    budget_set.add_argument("--limit-usd", required=True, help="e.g. 0.01, 500")
    budget_set.add_argument(
        "--db", default="./inferrail-budgets.db",
        help="Path to the budget store (default: %(default)s).",
    )

    budget_list = budget_sub.add_parser("list", help="List every configured budget.")
    budget_list.add_argument(
        "--db", default="./inferrail-budgets.db",
        help="Path to the budget store (default: %(default)s).",
    )
    budget_list.add_argument("--json", action="store_true", help="Print budgets as JSON.")

    budget_rm = budget_sub.add_parser(
        "rm", help="Remove a budget by id (as shown by 'budget list')."
    )
    budget_rm.add_argument("budget_id")
    budget_rm.add_argument(
        "--db", default="./inferrail-budgets.db",
        help="Path to the budget store (default: %(default)s).",
    )

    pricing = subparsers.add_parser("pricing", help="Pricing utilities.")
    pricing_sub = pricing.add_subparsers(dest="pricing_command", required=True)
    pricing_sub.add_parser(
        "update",
        help="Report built-in pricing catalog freshness (never fetches over the network).",
    )

    doctor = subparsers.add_parser(
        "doctor", help="Check port availability, pricing freshness, and provider reachability."
    )
    doctor.add_argument(
        "--config", default="inferrail.yaml", help="Path to inferrail.yaml (default: %(default)s)"
    )

    return parser


def _resolve_receipts_path(args: argparse.Namespace) -> Path | None:
    """Shared by `report` and `transaction`: resolve the receipts JSONL
    path from `--receipts`, or `--config`'s `receipts.path`, or (if
    neither was given and no `inferrail.yaml` exists) the quickstart
    default. Returns `None`, having already printed the error, if an
    explicitly-given `--config` fails to load.
    """
    if args.receipts is not None:
        return Path(args.receipts)
    if args.config is None and not Path("inferrail.yaml").exists():
        # No explicit --config, and no default inferrail.yaml to read
        # receipts.path from — fall back to the same default
        # `inferrail try`/`inferrail serve --quickstart` write to, so
        # `inferrail report --by customer` (or `inferrail transaction ...`)
        # works immediately after the quickstart path with no extra flags.
        return Path(QUICKSTART_RECEIPTS_PATH)
    try:
        config = load_config(args.config or "inferrail.yaml")
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    return Path(config.receipts.path)


@dataclass(frozen=True)
class _AppModePaths:
    app_data_dir: Path
    receipts: Path
    budgets: Path
    outcomes: Path
    token_file: Path
    ap_recovery: Path


def _apply_app_mode(config: InferrailConfig) -> tuple[InferrailConfig, _AppModePaths]:
    """`--app-mode` relocates receipts/budgets under the OS app-data
    directory and forces `receipts.sink: sqlite` /
    `budgets.enabled: true`, regardless of what `inferrail.yaml` says —
    see docs/adr/0016-local-control-api.md. Providers/routes/telemetry
    are untouched: app-mode is about *where local data lives and how
    it's exposed*, not which upstream providers are configured.

    `ap_recovery` defaults to a fixed path under the app-data directory,
    same treatment as receipts/budgets — but unlike those, the AP module
    (`inferrail ap demo|report|outcome`) has no config-file wiring at
    all today; a user with an existing recovery store elsewhere points
    at it via `INFERRAIL_AP_DB` rather than a CLI flag, kept minimal
    since this is the Recover screen's only consumer so far (see
    docs/PRODUCT.md's "Dashboard" section).
    """
    app_data = ensure_app_data_dir()
    paths = _AppModePaths(
        app_data_dir=app_data,
        receipts=app_data / "receipts.db",
        budgets=app_data / "budgets.db",
        outcomes=app_data / "work-outcomes.jsonl",
        token_file=app_data / "local-api-token",
        ap_recovery=Path(os.environ.get("INFERRAIL_AP_DB", str(app_data / "ap-recovery.db"))),
    )
    config.receipts = ReceiptsConfig(sink="sqlite", path=str(paths.receipts))
    config.budgets = BudgetsConfig(enabled=True, path=str(paths.budgets))
    return config, paths


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from inferrail.gateway.app import create_app

    if args.quickstart and args.app_mode:
        print("error: --quickstart and --app-mode are not combinable", file=sys.stderr)
        return 1

    app_mode_paths = None
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
        if args.app_mode:
            config, app_mode_paths = _apply_app_mode(config)
        app = create_app(
            config,
            app_mode=args.app_mode,
            local_outcomes_path=app_mode_paths.outcomes if app_mode_paths else None,
            ap_recovery_path=app_mode_paths.ap_recovery if app_mode_paths else None,
        )
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    host = args.host or config.server.host
    port = args.port or config.server.port

    if app_mode_paths is not None:
        print(f"App-mode data directory: {app_mode_paths.app_data_dir}")
        print(f"  receipts: {app_mode_paths.receipts}")
        print(f"  budgets:  {app_mode_paths.budgets}")
        print(f"  outcomes: {app_mode_paths.outcomes}")
        print(f"  ap recovery: {app_mode_paths.ap_recovery}")
        print(
            "    (point 'inferrail ap demo|report|outcome --db "
            f"{app_mode_paths.ap_recovery}' at this path to use the "
            "dashboard's Recover screen; override with INFERRAIL_AP_DB)"
        )
        print(f"Local control API token (also saved at {app_mode_paths.token_file}):")
        print(f"  {app.state.local_api_token}")
        # See docs/adr/0017-dashboard-in-app-directory.md for why the
        # token travels in this URL (Jupyter-style: "zero terminal use
        # after startup" means the printed link alone must be enough).
        if app.state.dashboard_dist is not None:
            print(f"Dashboard: http://{host}:{port}/dashboard/?token={app.state.local_api_token}")
        else:
            print(
                "Dashboard: not built yet -- run 'cd app && npm install && npm run build', "
                "then restart 'inferrail serve --app-mode'"
            )
        print()

    uvicorn.run(app, host=host, port=port)
    return 0


def _cmd_config_check(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        build_providers(config)
        build_anthropic_providers(config)
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
    print(f"  budgets:          {'enabled' if config.budgets.enabled else 'disabled'}")
    print(f"  server:           {config.server.host}:{config.server.port}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    receipts_path = _resolve_receipts_path(args)
    if receipts_path is None:
        return 1
    return run_report(receipts_path, args.by)


def _cmd_transaction(args: argparse.Namespace) -> int:
    receipts_path = _resolve_receipts_path(args)
    if receipts_path is None:
        return 1
    return run_transaction(receipts_path, args.task_id, args.attribute_name, as_json=args.json)


def _cmd_work(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    outcomes_path = Path(args.outcomes)
    if args.work_or_action == "outcome":
        if args.outcome_work_id is None or args.status is None:
            parser.error("'inferrail work outcome' requires WORK_ID and --status STATUS")
        return run_work_outcome(outcomes_path, args.outcome_work_id, args.status)
    if args.all:
        if args.work_or_action is not None or args.outcome_work_id is not None:
            parser.error("--all does not take a work id")
        receipts_path = _resolve_receipts_path(args)
        if receipts_path is None:
            return 1
        return run_work(receipts_path, outcomes_path, None, all_work=True, as_json=args.json)
    if args.work_or_action is None or args.outcome_work_id is not None:
        parser.error(
            "use 'inferrail work WORK_ID', 'inferrail work outcome WORK_ID --status STATUS', "
            "or 'inferrail work --all'"
        )
    receipts_path = _resolve_receipts_path(args)
    if receipts_path is None:
        return 1
    return run_work(
        receipts_path, outcomes_path, args.work_or_action, all_work=False, as_json=args.json
    )


def _cmd_demo(args: argparse.Namespace) -> int:
    del args  # no arguments
    return run_demo()


def _cmd_receipts(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.receipts_command == "import":
        return run_receipts_import(Path(args.jsonl), Path(args.db))
    if args.receipts_command == "export":
        return run_receipts_export(Path(args.db), Path(args.jsonl))
    parser.error(f"unknown receipts subcommand: {args.receipts_command}")
    return 1


def _cmd_ap(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.ap_command == "demo":
        return run_ap_demo()
    if args.ap_command == "report":
        return run_ap_report(Path(args.db), as_json=args.json)
    if args.ap_command == "outcome":
        return run_ap_outcome(
            Path(args.db),
            args.work_id,
            args.outcome,
            correction_delta_usd=args.correction_delta_usd,
            review_cost_usd=args.review_cost_usd,
            source=args.source,
        )
    if args.ap_command == "reap":
        return run_ap_reap(Path(args.db), as_json=args.json)
    if args.ap_command == "batch":
        return run_ap_batch(
            Path(args.attempts),
            Path(args.reviews),
            Path(args.out),
            vendor_confidence_threshold=args.vendor_confidence_threshold,
            candidate_human_review_threshold=args.candidate_human_review_threshold,
            candidate_retry_floor=args.candidate_retry_floor,
            candidate_max_prior_attempts=args.candidate_max_prior_attempts,
        )
    parser.error(f"unknown ap subcommand: {args.ap_command}")
    return 1


def _cmd_budget(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.budget_command == "set":
        return run_budget_set(
            Path(args.db),
            scope=args.scope,
            scope_value=args.scope_value,
            window=args.window,
            mode=args.mode,
            limit_usd=args.limit_usd,
        )
    if args.budget_command == "list":
        return run_budget_list(Path(args.db), as_json=args.json)
    if args.budget_command == "rm":
        return run_budget_rm(Path(args.db), args.budget_id)
    parser.error(f"unknown budget subcommand: {args.budget_command}")
    return 1


def _cmd_try(args: argparse.Namespace) -> int:
    return run_try(
        args.prompt,
        customer=args.customer,
        workflow=args.workflow,
        attribute_args=args.attributes,
        model=args.model,
    )


def _cmd_pricing(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.pricing_command == "update":
        return run_pricing_update()
    parser.error(f"unknown pricing subcommand: {args.pricing_command}")
    return 1


def _cmd_doctor(args: argparse.Namespace) -> int:
    return run_doctor(args.config)


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
    if args.command == "transaction":
        return _cmd_transaction(args)
    if args.command == "work":
        return _cmd_work(args, parser)
    if args.command == "demo":
        return _cmd_demo(args)
    if args.command == "try":
        return _cmd_try(args)
    if args.command == "ap":
        return _cmd_ap(args, parser)
    if args.command == "receipts":
        return _cmd_receipts(args, parser)
    if args.command == "budget":
        return _cmd_budget(args, parser)
    if args.command == "pricing":
        return _cmd_pricing(args, parser)
    if args.command == "doctor":
        return _cmd_doctor(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
