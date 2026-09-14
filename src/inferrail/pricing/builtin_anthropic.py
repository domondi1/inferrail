"""The built-in, independently-verified Anthropic pricing catalog.

Every entry here was checked against Anthropic's own published model/
pricing page (not a blog, aggregator, or remembered value) on
`verified_date`, for the standard (non-batch, non-cached-input) tier.
Prices change over time — `verified_date` and `source` exist so a stale
entry can be spotted and updated deliberately, and so an old
`InferenceReceipt` remains self-explanatory even after this catalog
moves on (the receipt embeds a copy of the `PriceEntry` used, not a
reference to this module).

This catalog is deliberately small, mirroring
`inferrail.pricing.builtin`'s own restraint — only the current model
generation, not every historical/legacy model id Anthropic still serves.
Do not add a model here from memory or a third-party source — see
docs/adr/0005-privacy-preserving-economic-receipts.md for why unverified
pricing is worse than no pricing.

Only applied to providers configured as `type: anthropic` with no custom
`base_url` — see `inferrail.pricing.resolver.PricingResolver` for why an
`anthropic_compatible` endpoint (which may be a completely different
backend that merely speaks the same wire format) never gets these prices
by default.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from inferrail.config.models import PriceEntry

_ANTHROPIC_PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/models/overview"
_VERIFIED_DATE = date(2026, 9, 14)


def _price(input_usd: str, output_usd: str) -> PriceEntry:
    return PriceEntry(
        input_usd_per_million=Decimal(input_usd),
        output_usd_per_million=Decimal(output_usd),
        source=_ANTHROPIC_PRICING_SOURCE,
        verified_date=_VERIFIED_DATE,
    )


# No current Claude model has context-length-tiered pricing (unlike some
# OpenAI flagships excluded from BUILTIN_OPENAI_PRICING for that reason) —
# every entry here is a single, unconditional input/output rate.
#
# claude-haiku-4-5 is both a dateless alias and its own pinned dated
# snapshot (claude-haiku-4-5-20251001); both are listed since a caller
# may legitimately send either and both bill at the same rate.
BUILTIN_ANTHROPIC_PRICING: dict[str, PriceEntry] = {
    "claude-fable-5-1": _price("10.00", "50.00"),
    "claude-opus-5": _price("5.00", "25.00"),
    "claude-sonnet-5": _price("2.00", "10.00"),
    "claude-haiku-4-5": _price("1.00", "5.00"),
    "claude-haiku-4-5-20251001": _price("1.00", "5.00"),
}
