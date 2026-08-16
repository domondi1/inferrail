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
_VERIFIED_DATE = date(2026, 8, 16)

BUILTIN_OPENAI_PRICING: dict[str, PriceEntry] = {
    "gpt-4o-mini": PriceEntry(
        input_usd_per_million=Decimal("0.15"),
        output_usd_per_million=Decimal("0.60"),
        source=_OPENAI_PRICING_SOURCE,
        verified_date=_VERIFIED_DATE,
    ),
    "gpt-4o": PriceEntry(
        input_usd_per_million=Decimal("2.50"),
        output_usd_per_million=Decimal("10.00"),
        source=_OPENAI_PRICING_SOURCE,
        verified_date=_VERIFIED_DATE,
    ),
}
