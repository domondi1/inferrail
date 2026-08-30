"""Shared, deterministic economics derived from a sequence of receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from inferrail.receipts.schema import InferenceReceipt


@dataclass(frozen=True)
class ReceiptEconomics:
    known_cost_usd: Decimal
    unknown_cost_count: int
    status: Literal["success", "partial", "error", "unknown"]
    started_at: datetime | None
    ended_at: datetime | None


def summarize_receipts(receipts: list[InferenceReceipt]) -> ReceiptEconomics:
    """Compute the shared cost, status, and time semantics for receipts."""
    if not receipts:
        return ReceiptEconomics(Decimal("0"), 0, "unknown", None, None)

    ordered = sorted(receipts, key=lambda receipt: receipt.timestamp)
    known_cost_usd = Decimal("0")
    unknown_cost_count = 0
    for receipt in ordered:
        if receipt.estimated_cost_usd is not None:
            known_cost_usd += receipt.estimated_cost_usd
        elif receipt.status == "success":
            unknown_cost_count += 1

    statuses = {receipt.status for receipt in ordered}
    if statuses == {"success"}:
        status: Literal["success", "partial", "error"] = "success"
    elif statuses == {"error"}:
        status = "error"
    else:
        status = "partial"
    return ReceiptEconomics(
        known_cost_usd, unknown_cost_count, status, ordered[0].timestamp, ordered[-1].timestamp
    )
