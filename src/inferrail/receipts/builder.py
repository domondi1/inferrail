"""Assembles an `InferenceReceipt` from what the execution engine already
measured, resolving price and computing cost along the way.

Kept separate from `gateway.execution.InferenceEngine` so the
usage -> price -> cost -> receipt chain is one small, independently
testable unit (see docs/PRINCIPLES.md's "deterministic, testable
behavior"), and so `InferenceEngine` itself stays a thin orchestrator
rather than absorbing pricing/cost logic directly.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from inferrail.pricing.resolver import PricingResolver
from inferrail.receipts.calculator import CacheTokens, calculate_cost_with_cache_usd
from inferrail.receipts.schema import InferenceReceipt


def new_receipt_id() -> str:
    return f"ir_{uuid.uuid4().hex[:20]}"


def build_receipt(
    *,
    receipt_id: str,
    request_id: str,
    route: str,
    provider: str,
    model: str,
    status: Literal["success", "error", "partial"],
    prompt_tokens: int | None,
    completion_tokens: int | None,
    attributes: dict[str, str],
    total_latency_ms: float,
    retry_count: int,
    pricing_resolver: PricingResolver,
    cache: CacheTokens | None = None,
) -> InferenceReceipt:
    """Build a receipt for one execution attempt.

    Cost is computed only when both token counts are known (i.e. a
    provider actually returned a usage block) *and* a verified price is
    available for the (provider, model) pair. Any other case — a failed
    request, a provider that omitted usage, or an unrecognized model —
    leaves `pricing`/`estimated_cost_usd` explicitly `None` rather than a
    fabricated `0`.

    `prompt_tokens` is total input. `cache` carries any prompt-cache
    tokens the provider reported inside that total; if they can't be
    priced exactly (see `calculate_cost_with_cache_usd`), cost *and*
    pricing are `None` -- a known-looking cost that silently leaves out
    cache tokens is worse than an honest unknown.
    """
    pricing = None
    cost: Decimal | None = None
    if prompt_tokens is not None and completion_tokens is not None:
        pricing = pricing_resolver.resolve(provider, model)
        if pricing is not None:
            cost = calculate_cost_with_cache_usd(
                prompt_tokens, completion_tokens, pricing, cache
            )
            if cost is None:
                pricing = None

    return InferenceReceipt(
        receipt_id=receipt_id,
        request_id=request_id,
        route=route,
        provider=provider,
        model=model,
        status=status,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_creation_input_tokens=cache.creation if cache is not None else None,
        cache_creation_5m_input_tokens=cache.creation_5m if cache is not None else None,
        cache_creation_1h_input_tokens=cache.creation_1h if cache is not None else None,
        cache_read_input_tokens=cache.read if cache is not None else None,
        pricing=pricing,
        estimated_cost_usd=cost,
        attributes=attributes,
        total_latency_ms=total_latency_ms,
        retry_count=retry_count,
    )
