"""The built-in, independently-verified OpenAI pricing catalog.

Every entry here was checked against OpenAI's own published pricing page
(not a blog, aggregator, or remembered value) on `verified_date`, for the
standard (non-batch, non-cached-input) tier. Prices change over time —
`verified_date` and `source` exist so a stale entry can be spotted and
updated deliberately, and so an old `InferenceReceipt` remains
self-explanatory even after this catalog moves on (the receipt embeds a
copy of the `PriceEntry` used, not a reference to this module).

This catalog is deliberately small. Do not add a model here from memory or
a third-party source — see docs/adr/0005-privacy-preserving-economic-receipts.md
for why unverified pricing is worse than no pricing (an explicit "unknown"
is honest; a guessed number is not).

Only applied to providers configured as `type: openai` with no custom
`base_url` — see `inferrail.pricing.resolver.PricingResolver` for why an
`openai_compatible` endpoint (which may be a completely different backend
that merely speaks the same wire format) never gets these prices by
default.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from inferrail.config.models import LongContextPrice, PriceEntry

_OPENAI_PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
_VERIFIED_DATE = date(2026, 10, 5)


def _price(input_usd: str, output_usd: str, cached_input_usd: str) -> PriceEntry:
    # OpenAI bills prompt tokens served from its automatic prompt cache
    # (`usage.prompt_tokens_details.cached_tokens`) at the "Cached input"
    # rate; stored as the entry's cache-read rate.
    return PriceEntry(
        input_usd_per_million=Decimal(input_usd),
        output_usd_per_million=Decimal(output_usd),
        cache_read_usd_per_million=Decimal(cached_input_usd),
        source=_OPENAI_PRICING_SOURCE,
        verified_date=_VERIFIED_DATE,
    )


def _tiered_price(
    input_usd: str,
    output_usd: str,
    cached_input_usd: str,
    long_input_usd: str,
    long_output_usd: str,
    long_cached_input_usd: str,
) -> PriceEntry:
    # OpenAI: "Short context: <=272K input tokens. Long context: >272K input
    # tokens", and the long rates apply "for the full request" (model pages).
    # 272_000, not 272 * 1024: if anything this switches tiers slightly
    # early, which overstates a cost rather than understating it.
    return _price(input_usd, output_usd, cached_input_usd).model_copy(
        update={
            "verified_date": date(2026, 10, 6),
            "long_context": LongContextPrice(
                above_input_tokens=272_000,
                input_usd_per_million=Decimal(long_input_usd),
                output_usd_per_million=Decimal(long_output_usd),
                cache_read_usd_per_million=Decimal(long_cached_input_usd),
            )
        }
    )


# Standard tier only: the `-pro`, batch, flex, and fast tiers are excluded.
# The gpt-5.6 flagships are context-tiered (a higher rate for the whole
# request above 272K input tokens), expressed with `long_context`.
BUILTIN_OPENAI_PRICING: dict[str, PriceEntry] = {
    "gpt-4o": _price("2.50", "10.00", "1.25"),
    "gpt-4o-mini": _price("0.15", "0.60", "0.075"),
    "gpt-4.1": _price("2.00", "8.00", "0.50"),
    "gpt-4.1-mini": _price("0.40", "1.60", "0.10"),
    "gpt-4.1-nano": _price("0.10", "0.40", "0.025"),
    "gpt-5": _price("1.25", "10.00", "0.125"),
    "gpt-5.1": _price("1.25", "10.00", "0.125"),
    "gpt-5-mini": _price("0.25", "2.00", "0.025"),
    "gpt-5-nano": _price("0.05", "0.40", "0.005"),
    "o3": _price("2.00", "8.00", "0.50"),
    "o4-mini": _price("1.10", "4.40", "0.275"),
    "gpt-5.6-sol": _tiered_price("4.00", "20.00", "0.40", "8.00", "30.00", "0.80"),
    "gpt-5.6-terra": _tiered_price("2.00", "12.00", "0.20", "4.00", "18.00", "0.40"),
    "gpt-5.6-luna": _tiered_price("0.20", "1.20", "0.02", "0.40", "1.80", "0.04"),
}
