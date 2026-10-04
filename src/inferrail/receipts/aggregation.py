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
    # Summed over receipts that reported usage; None when none did, so a
    # run with no token-bearing receipt reads as "unknown", not "0".
    prompt_tokens: int | None
    completion_tokens: int | None


def summarize_receipts(receipts: list[InferenceReceipt]) -> ReceiptEconomics:
    """Compute the shared cost, status, and time semantics for receipts."""
    if not receipts:
        return ReceiptEconomics(Decimal("0"), 0, "unknown", None, None, None, None)

    ordered = sorted(receipts, key=lambda receipt: receipt.timestamp)
    known_cost_usd = Decimal("0")
    unknown_cost_count = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    for receipt in ordered:
        if receipt.estimated_cost_usd is not None:
            known_cost_usd += receipt.estimated_cost_usd
        elif receipt.status == "success":
            unknown_cost_count += 1
        if receipt.prompt_tokens is not None:
            prompt_tokens = (prompt_tokens or 0) + receipt.prompt_tokens
        if receipt.completion_tokens is not None:
            completion_tokens = (completion_tokens or 0) + receipt.completion_tokens

    statuses = {receipt.status for receipt in ordered}
    if statuses == {"success"}:
        status: Literal["success", "partial", "error"] = "success"
    elif statuses == {"error"}:
        status = "error"
    else:
        status = "partial"
    return ReceiptEconomics(
        known_cost_usd,
        unknown_cost_count,
        status,
        ordered[0].timestamp,
        ordered[-1].timestamp,
        prompt_tokens,
        completion_tokens,
    )
