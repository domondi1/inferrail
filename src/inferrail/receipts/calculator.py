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

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from inferrail.config.models import PriceEntry

_MILLION = Decimal(1_000_000)
_QUANTUM = Decimal("0.000001")


def calculate_cost_usd(prompt_tokens: int, completion_tokens: int, price: PriceEntry) -> Decimal:
    input_cost = Decimal(prompt_tokens) * price.input_usd_per_million / _MILLION
    output_cost = Decimal(completion_tokens) * price.output_usd_per_million / _MILLION
    return (input_cost + output_cost).quantize(_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class CacheTokens:
    """Prompt-cache token counts a provider reported for one request.

    Anthropic reports these *outside* `input_tokens`
    (`total input = input_tokens + cache_creation_input_tokens +
    cache_read_input_tokens`), and bills them at different rates: a
    5-minute cache write, a 1-hour cache write, and a cache read each
    have their own price. `creation_5m`/`creation_1h` come from the
    `usage.cache_creation` breakdown; they are `None` when the provider
    didn't send it.
    """

    creation: int
    creation_5m: int | None
    creation_1h: int | None
    read: int

    @property
    def total(self) -> int:
        return self.creation + self.read

    @property
    def creation_split_known(self) -> bool:
        if self.creation == 0:
            return True
        return (
            self.creation_5m is not None
            and self.creation_1h is not None
            and self.creation_5m + self.creation_1h == self.creation
        )


def _nonneg_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def parse_anthropic_cache_usage(usage: Mapping[str, object]) -> CacheTokens | None:
    """Reads Anthropic's cache fields from a `usage` object. Returns `None`
    when the provider reported no cache fields at all (so a provider
    that doesn't do caching produces exactly the receipt it always did)."""
    has_creation = "cache_creation_input_tokens" in usage
    has_read = "cache_read_input_tokens" in usage
    if not has_creation and not has_read:
        return None
    creation = _nonneg_int(usage.get("cache_creation_input_tokens")) or 0
    read = _nonneg_int(usage.get("cache_read_input_tokens")) or 0
    creation_5m: int | None = None
    creation_1h: int | None = None
    breakdown = usage.get("cache_creation")
    if isinstance(breakdown, Mapping):
        creation_5m = _nonneg_int(breakdown.get("ephemeral_5m_input_tokens"))
        creation_1h = _nonneg_int(breakdown.get("ephemeral_1h_input_tokens"))
        if creation_5m is not None and creation_1h is None:
            creation_1h = 0
        if creation_1h is not None and creation_5m is None:
            creation_5m = 0
    return CacheTokens(
        creation=creation, creation_5m=creation_5m, creation_1h=creation_1h, read=read
    )


def calculate_cost_with_cache_usd(
    prompt_tokens: int,
    completion_tokens: int,
    price: PriceEntry,
    cache: CacheTokens | None,
) -> Decimal | None:
    """Like `calculate_cost_usd`, but `prompt_tokens` is the *total* input
    (uncached + cache writes + cache reads) and cache tokens are priced at
    their own rates.

    Returns `None` -- unknown, never a guess -- when cache tokens were
    reported but can't be priced exactly: the entry has no rate for them,
    or cache writes were reported without the 5-minute/1-hour split that
    decides which write rate applies.
    """
    if cache is None or cache.total == 0:
        return calculate_cost_usd(prompt_tokens, completion_tokens, price)
    uncached = prompt_tokens - cache.total
    if uncached < 0:
        return None
    total = Decimal(uncached) * price.input_usd_per_million
    total += Decimal(completion_tokens) * price.output_usd_per_million
    if cache.read:
        if price.cache_read_usd_per_million is None:
            return None
        total += Decimal(cache.read) * price.cache_read_usd_per_million
    if cache.creation:
        if not cache.creation_split_known:
            return None
        for tokens, rate in (
            (cache.creation_5m or 0, price.cache_write_5m_usd_per_million),
            (cache.creation_1h or 0, price.cache_write_1h_usd_per_million),
        ):
            if tokens:
                if rate is None:
                    return None
                total += Decimal(tokens) * rate
    return (total / _MILLION).quantize(_QUANTUM, rounding=ROUND_HALF_UP)
