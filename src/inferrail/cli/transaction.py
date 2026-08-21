"""`inferrail transaction`: show one task's aggregated economic
transaction — every receipt sharing one attribution-attribute value,
grouped into a single `TaskTransaction`.

Kept independent of `argparse`/stdout for the same reason as `cli.report`
(see docs/PRINCIPLES.md's "deterministic, testable behavior").
"""

from __future__ import annotations

import sys
from pathlib import Path

from inferrail.cli.report import format_usd, load_receipts
from inferrail.transactions.builder import build_transaction
from inferrail.transactions.schema import TaskTransaction


def format_transaction(transaction: TaskTransaction) -> str:
    lines = [
        f"Task:        {transaction.task_id}",
        f"Transaction: {transaction.transaction_id}",
        f"Status:      {transaction.status}",
        "",
    ]

    columns = ["EVENT TYPE", "EVENT ID", "STATUS", "COST"]
    rows = [
        [
            e.event_type,
            e.event_id,
            e.status,
            "unknown" if e.cost_usd is None else format_usd(e.cost_usd),
        ]
        for e in transaction.events
    ]
    if rows:
        widths = [max(len(col), *(len(row[i]) for row in rows)) for i, col in enumerate(columns)]
    else:
        widths = [len(col) for col in columns]

    def _format_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True))

    lines.append(_format_row(columns))
    lines.extend(_format_row(row) for row in rows)
    lines.append("")

    known_total = format_usd(transaction.known_total_cost_usd)
    if transaction.known_total_cost_usd == 0 and transaction.unknown_cost_event_count > 0:
        known_total = "unknown"
    lines.append(f"Known total cost: {known_total}")
    if transaction.unknown_cost_event_count:
        lines.append(f"Unknown-cost events: {transaction.unknown_cost_event_count}")
    lines.append(f"Started: {transaction.started_at.isoformat()}")
    lines.append(f"Ended:   {transaction.ended_at.isoformat()}")
    return "\n".join(lines)


def run_transaction(path: Path, task_id: str, attribute_name: str, *, as_json: bool) -> int:
    if not path.exists():
        print(
            f"No receipts found at {path}. Run some requests through the "
            "gateway first (see receipts.sink in inferrail.yaml).",
            file=sys.stderr,
        )
        return 0

    receipts, skipped = load_receipts(path)
    transaction = build_transaction(task_id, receipts, attribute_name=attribute_name)

    if transaction is None:
        print(
            f"No transaction found: no receipt has attribute "
            f"'{attribute_name}={task_id}'."
        )
        if skipped:
            print(f"\nSkipped {skipped} malformed/unrecognized receipt row(s).", file=sys.stderr)
        return 0

    if as_json:
        print(transaction.model_dump_json(indent=2))
    else:
        print(format_transaction(transaction))
    if skipped:
        print(f"\nSkipped {skipped} malformed/unrecognized receipt row(s).", file=sys.stderr)
    return 0
