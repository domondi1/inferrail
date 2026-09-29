"""The built-in, independently-verified Anthropic pricing catalog.

Every entry here was checked against Anthropic's own published model/
pricing page (not a blog, aggregator, or remembered value) on
`verified_date`, for the standard (non-batch) tier, including the
prompt-cache rates (5-minute cache write, 1-hour cache write, cache
read) so a request that uses prompt caching is priced exactly rather
than having its cache tokens dropped. Fast mode and the `inference_geo`
data-residency multiplier are not modeled.
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

_ANTHROPIC_PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
_VERIFIED_DATE = date(2026, 9, 29)


def _price(
    input_usd: str,
    output_usd: str,
    *,
    cache_write_5m_usd: str,
    cache_write_1h_usd: str,
    cache_read_usd: str,
) -> PriceEntry:
    return PriceEntry(
        input_usd_per_million=Decimal(input_usd),
        output_usd_per_million=Decimal(output_usd),
        cache_write_5m_usd_per_million=Decimal(cache_write_5m_usd),
        cache_write_1h_usd_per_million=Decimal(cache_write_1h_usd),
        cache_read_usd_per_million=Decimal(cache_read_usd),
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
    # Cache reads on Claude Fable 5.1 are 0.025x base input, not the usual
    # 0.1x (per the pricing page's own footnote).
    "claude-fable-5-1": _price(
        "10.00", "50.00", cache_write_5m_usd="12.50", cache_write_1h_usd="20.00",
        cache_read_usd="0.25",
    ),
    "claude-opus-5": _price(
        "5.00", "25.00", cache_write_5m_usd="6.25", cache_write_1h_usd="10.00",
        cache_read_usd="0.50",
    ),
    "claude-sonnet-5": _price(
        "2.00", "10.00", cache_write_5m_usd="2.50", cache_write_1h_usd="4.00",
        cache_read_usd="0.20",
    ),
    "claude-haiku-4-5": _price(
        "1.00", "5.00", cache_write_5m_usd="1.25", cache_write_1h_usd="2.00",
        cache_read_usd="0.10",
    ),
    "claude-haiku-4-5-20251001": _price(
        "1.00", "5.00", cache_write_5m_usd="1.25", cache_write_1h_usd="2.00",
        cache_read_usd="0.10",
    ),
}
