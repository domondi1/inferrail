"""Shared, deterministic economics derived from a sequence of receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from inferrail.receipts.classification import is_simulated
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
    # Real vs simulated (see receipts.classification). Every cost and token
    # field above covers *real* receipts only; a simulated receipt's
    # illustrative price is reported here and never added to real spend.
    # `status`, `started_at` and `ended_at` still cover every receipt.
    real_receipt_count: int = 0
    simulated_receipt_count: int = 0
    simulated_cost_usd: Decimal = Decimal("0")


def summarize_receipts(receipts: list[InferenceReceipt]) -> ReceiptEconomics:
    """Compute the shared cost, status, and time semantics for receipts."""
    if not receipts:
        return ReceiptEconomics(Decimal("0"), 0, "unknown", None, None, None, None)

    ordered = sorted(receipts, key=lambda receipt: receipt.timestamp)
    known_cost_usd = Decimal("0")
    unknown_cost_count = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    real_count = simulated_count = 0
    simulated_cost_usd = Decimal("0")
    for receipt in ordered:
        if is_simulated(receipt):
            simulated_count += 1
            if receipt.estimated_cost_usd is not None:
                simulated_cost_usd += receipt.estimated_cost_usd
            continue
        real_count += 1
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
        real_receipt_count=real_count,
        simulated_receipt_count=simulated_count,
        simulated_cost_usd=simulated_cost_usd,
    )
