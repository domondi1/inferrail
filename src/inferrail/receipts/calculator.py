"""Deterministic token-usage -> cost arithmetic.

Isolated from pricing lookup (`inferrail.pricing.resolver`) and from receipt
assembly (`inferrail.receipts.builder`) so each is independently testable —
see docs/PRINCIPLES.md's "deterministic, testable behavior".

All arithmetic is `Decimal`; nothing here ever touches `float`. The result
is quantized to six decimal places (matching the catalog's finest published
granularity of a hundredth of a cent per million tokens) using
`ROUND_HALF_UP`, so identical inputs always produce an identical, auditable
output.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from inferrail.config.models import PriceEntry

_MILLION = Decimal(1_000_000)
_QUANTUM = Decimal("0.000001")


def calculate_cost_usd(prompt_tokens: int, completion_tokens: int, price: PriceEntry) -> Decimal:
    input_cost = Decimal(prompt_tokens) * price.input_usd_per_million / _MILLION
    output_cost = Decimal(completion_tokens) * price.output_usd_per_million / _MILLION
    return (input_cost + output_cost).quantize(_QUANTUM, rounding=ROUND_HALF_UP)
