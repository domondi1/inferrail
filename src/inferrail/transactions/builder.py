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

import hashlib
from collections.abc import Iterable
from typing import Literal

from inferrail.receipts.aggregation import summarize_receipts
from inferrail.receipts.schema import InferenceReceipt
from inferrail.transactions.schema import EconomicEventRef, TaskTransaction


def new_transaction_id(task_id: str, event_ids: Iterable[str]) -> str:
    """Derive a stable `transaction_id` from the task's own identity and the
    ids of the events that make it up.

    Deliberately not a random id: `inferrail transaction <task_id>` is a
    read-side view recomputed from receipts on every invocation (ADR-0008),
    so two reads of the same, unchanged task must return the same id — it
    needs to be citable in a log line or ticket. `event_ids` is sorted so
    the id doesn't depend on receipt read/iteration order; a *new* event
    joining the task set intentionally changes the id, since that's a
    different transaction than before.
    """
    digest_input = "\n".join([task_id, *sorted(event_ids)])
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
    return f"tx_{digest[:20]}"


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
    matching.sort(key=lambda receipt: receipt.timestamp)
    events: list[EconomicEventRef] = []
    for receipt in matching:
        events.append(
            EconomicEventRef(
                event_type="inference",
                event_id=receipt.receipt_id,
                cost_usd=receipt.estimated_cost_usd,
                status=receipt.status,
            )
        )
    economics = summarize_receipts(matching)
    assert economics.status != "unknown"
    assert economics.started_at is not None
    assert economics.ended_at is not None
    status: Literal["success", "partial", "error"] = economics.status

    return TaskTransaction(
        transaction_id=new_transaction_id(task_id, (e.event_id for e in events)),
        task_id=task_id,
        events=events,
        known_total_cost_usd=economics.known_cost_usd,
        unknown_cost_event_count=economics.unknown_cost_count,
        status=status,
        started_at=economics.started_at,
        ended_at=economics.ended_at,
    )
