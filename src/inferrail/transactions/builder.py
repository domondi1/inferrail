"""Assembles a `TaskTransaction` from existing `InferenceReceipt`s that
share one attribution-attribute value.

Deliberately a pure aggregation over already-produced receipts — see
docs/adr/0008-task-transactions.md for why this doesn't touch the gateway
request path or require any new attribution mechanism: a caller that
wants receipts grouped into one task already has everything needed by
attaching an `X-Inferrail-Attribute-<name>` header (see
gateway.attribution) to every request that belongs to that task.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from inferrail.receipts.schema import InferenceReceipt
from inferrail.transactions.schema import EconomicEventRef, TaskTransaction


def new_transaction_id() -> str:
    return f"tx_{uuid.uuid4().hex[:20]}"


def build_transaction(
    task_id: str,
    receipts: list[InferenceReceipt],
    *,
    attribute_name: str = "task_id",
) -> TaskTransaction | None:
    """Build the `TaskTransaction` for one task from a pool of receipts.

    `receipts` is the full, unfiltered pool (e.g. everything
    `cli.report.load_receipts` read from a JSONL file) — this function
    does the filtering itself, matching each receipt's
    `attributes[attribute_name]` against `task_id`. Returns `None` if no
    receipt matches: "no such task" is a real, distinct outcome from "a
    task whose every event is unpriced," which still returns a
    transaction (with `known_total_cost_usd == 0` and a nonzero
    `unknown_cost_event_count`).
    """
    matching = [r for r in receipts if r.attributes.get(attribute_name) == task_id]
    if not matching:
        return None
    matching.sort(key=lambda r: r.timestamp)

    events: list[EconomicEventRef] = []
    known_total = Decimal("0")
    unknown_cost_event_count = 0
    for receipt in matching:
        events.append(
            EconomicEventRef(
                event_type="inference",
                event_id=receipt.receipt_id,
                cost_usd=receipt.estimated_cost_usd,
                status=receipt.status,
            )
        )
        if receipt.estimated_cost_usd is not None:
            known_total += receipt.estimated_cost_usd
        elif receipt.status == "success":
            # Only a successful receipt with unresolvable pricing is a
            # genuine pricing gap — an error/partial receipt legitimately
            # has no cost to know, mirroring cli.report.aggregate.
            unknown_cost_event_count += 1

    statuses = {e.status for e in events}
    status: Literal["success", "partial", "error"]
    if statuses == {"success"}:
        status = "success"
    elif statuses == {"error"}:
        status = "error"
    else:
        status = "partial"

    return TaskTransaction(
        transaction_id=new_transaction_id(),
        task_id=task_id,
        events=events,
        known_total_cost_usd=known_total,
        unknown_cost_event_count=unknown_cost_event_count,
        status=status,
        started_at=matching[0].timestamp,
        ended_at=matching[-1].timestamp,
    )
