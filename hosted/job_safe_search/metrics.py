"""External-customer revenue and contribution metrics for the search ledger."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from .contract import atomic, usd

SETTLED_STATES = {
    "SUPPLIER_INFLIGHT",
    "SUPPLIER_UNKNOWN",
    "SERVICE_FAILED",
    "DELIVERED",
    "RESOLVED",
}
RESOLVED_STATES = {"DELIVERED", "RESOLVED"}


def excluded_wallets(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    return {
        line.split("#", 1)[0].strip().lower()
        for line in path.read_text().splitlines()
        if line.split("#", 1)[0].strip()
    }


def report(
    db_path: Path, excluded: set[str], hosting_cost_usd: str | None = None
) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute("SELECT * FROM purchases")]
        events = [dict(row) for row in conn.execute("SELECT * FROM events")]

    external = [row for row in rows if row["payer"].lower() not in excluded]
    paid = [row for row in external if row["state"] in SETTLED_STATES and row["tx"] is not None]
    delivered = [row for row in paid if row["state"] in RESOLVED_STATES]
    wallets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in delivered:
        wallets[row["payer"].lower()].append(row)

    eligible = {row["id"] for row in external}
    cache_events = [
        event for event in events if event["kind"] == "CACHE_HIT" and event["purchase"] in eligible
    ]
    known_cogs = [row["supplier_cogs"] for row in paid if row["supplier_cogs"] is not None]
    known_fees = [row["variable_fees"] for row in paid if row["variable_fees"] is not None]
    known_refunds = [row["refunds"] for row in paid]
    known_credits = [row["credits"] for row in paid]
    settled_revenue = sum(row["price"] for row in paid)
    margin_rows = [
        row
        for row in delivered
        if row["supplier_cogs"] is not None
        and row["variable_fees"] is not None
        and row["liability"] == 0
    ]
    margins = [
        row["price"]
        - row["supplier_cogs"]
        - row["refunds"]
        - row["credits"]
        - row["variable_fees"]
        for row in margin_rows
    ]
    returned_later = [
        items
        for items in wallets.values()
        if len(items) >= 2
        and max(row["created"] for row in items) - min(row["created"] for row in items) >= 86400
    ]
    unresolved = [
        row
        for row in external
        if row["state"] not in RESOLVED_STATES | {"PAYMENT_REJECTED"}
        or (
            row["state"] in RESOLVED_STATES
            and (row["supplier_cogs"] is None or row["variable_fees"] is None)
        )
    ]
    blocked_fallbacks = sum(event["kind"] == "BLOCKED_UNSAFE_FALLBACK" for event in events)

    return {
        "external_paid_calls": len(paid),
        "external_successful_paid_calls": len(delivered),
        "external_unique_wallets": len({row["payer"].lower() for row in paid}),
        "wallets_with_3_plus_paid_calls": sum(len(items) >= 3 for items in wallets.values()),
        "wallets_with_3_plus_distinct_queries": sum(
            len({row["fingerprint"] for row in items}) >= 3 for items in wallets.values()
        ),
        "wallets_returning_after_24h": len(returned_later),
        "gross_external_settled_revenue_usd": usd(settled_revenue),
        "supplier_cogs_usd": usd(sum(known_cogs)),
        "refunds_usd": usd(sum(known_refunds)),
        "credits_usd": usd(sum(known_credits)),
        "variable_payment_fees_usd": usd(sum(known_fees)),
        "realized_external_contribution_margin_usd": usd(sum(margins)),
        "hosting_infrastructure_cost_usd": hosting_cost_usd,
        "strict_experiment_pnl_usd": (
            usd(sum(margins) - atomic(hosting_cost_usd))
            if hosting_cost_usd is not None
            else None
        ),
        "average_realized_margin_per_resolved_call_usd": (
            usd(sum(margins) // len(margins)) if margins else None
        ),
        "negative_margin_requests": sum(value < 0 for value in margins),
        "cache_hits": len(cache_events),
        "supplier_cogs_avoided_on_cache_usd": usd(
            sum(json.loads(event["details"]).get("cogs_avoided", 0) for event in cache_events)
        ),
        "safe_fallbacks": 0,
        "blocked_unsafe_fallbacks": blocked_fallbacks,
        "failed_no_charge_requests": sum(row["state"] == "PAYMENT_REJECTED" for row in external),
        "unresolved_transactions": len(unresolved),
        "unknown_supplier_cogs_calls": sum(row["supplier_cogs"] is None for row in paid),
        "unknown_variable_fee_calls": sum(row["variable_fees"] is None for row in paid),
        "excluded_wallet_count": len(excluded),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--exclude-wallets", type=Path, required=True)
    parser.add_argument("--hosting-cost-usd")
    args = parser.parse_args()
    print(
        json.dumps(
            report(
                args.database,
                excluded_wallets(args.exclude_wallets),
                hosting_cost_usd=args.hosting_cost_usd,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
