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

from inferrail.config.models import PriceEntry

_OPENAI_PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
_VERIFIED_DATE = date(2026, 8, 22)


def _price(input_usd: str, output_usd: str) -> PriceEntry:
    return PriceEntry(
        input_usd_per_million=Decimal(input_usd),
        output_usd_per_million=Decimal(output_usd),
        source=_OPENAI_PRICING_SOURCE,
        verified_date=_VERIFIED_DATE,
    )


# Every entry is a model whose standard-tier price is a single
# input/output pair. Deliberately excluded, even though they're current
# flagships: `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` are priced
# per *context length* (a higher rate above 270K tokens), which a single
# `PriceEntry` cannot represent — storing only the short-context rate
# would silently under-report a long-context request's real cost, which is
# precisely the fabricated-precision failure this catalog exists to avoid.
# They resolve to `null` (an honest "unknown") until either the schema
# models context-tiered pricing or an operator supplies a `pricing:`
# override they've chosen themselves. Same reasoning excludes the `-pro`,
# batch, flex, and fast tiers.
BUILTIN_OPENAI_PRICING: dict[str, PriceEntry] = {
    "gpt-4o": _price("2.50", "10.00"),
    "gpt-4o-mini": _price("0.15", "0.60"),
    "gpt-4.1": _price("2.00", "8.00"),
    "gpt-4.1-mini": _price("0.40", "1.60"),
    "gpt-4.1-nano": _price("0.10", "0.40"),
    "gpt-5": _price("1.25", "10.00"),
    "gpt-5.1": _price("1.25", "10.00"),
    "gpt-5-mini": _price("0.25", "2.00"),
    "gpt-5-nano": _price("0.05", "0.40"),
    "o3": _price("2.00", "8.00"),
    "o4-mini": _price("1.10", "4.40"),
}
