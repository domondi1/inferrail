"""`inferrail pricing update` — see docs/adr/0016-local-control-api.md's
"Also in this unit" note, and docs/adr/0005-privacy-preserving-economic-receipts.md
for why Inferrail never fetches pricing data over the network.

There is nothing to "update" at runtime: every built-in price is a
value a human verified by hand against the vendor's own pricing page
and shipped inside the installed package (`pricing/builtin.py`,
`pricing/builtin_anthropic.py`). This command is a diagnostic — it
reports how old each catalog is and states the one real fix (upgrade
the package, or add an explicit override) — never a silent background
fetch that could also silently drift from what a receipt says it used.
"""

from __future__ import annotations

from datetime import date

from inferrail.config.models import PriceEntry
from inferrail.pricing.builtin import BUILTIN_OPENAI_PRICING
from inferrail.pricing.builtin_anthropic import BUILTIN_ANTHROPIC_PRICING

#: Purely a reporting threshold — vendors don't publish on any fixed
#: cadence, so this doesn't mean "wrong", only "worth a human glance".
STALE_AFTER_DAYS = 90

_CATALOGS: dict[str, dict[str, PriceEntry]] = {
    "OpenAI": BUILTIN_OPENAI_PRICING,
    "Anthropic": BUILTIN_ANTHROPIC_PRICING,
}


def _oldest_verified_date(catalog: dict[str, PriceEntry]) -> date | None:
    dates = [entry.verified_date for entry in catalog.values()]
    return min(dates) if dates else None


def catalog_freshness(
    today: date | None = None,
) -> list[tuple[str, int, date | None, int | None, bool]]:
    """Returns `(catalog_name, model_count, oldest_verified_date,
    age_days, is_stale)` for each built-in catalog — shared by
    `run_pricing_update` and `inferrail doctor` so both report freshness
    identically instead of two slightly-different implementations."""
    today = today or date.today()
    results = []
    for name, catalog in _CATALOGS.items():
        oldest = _oldest_verified_date(catalog)
        age_days = (today - oldest).days if oldest is not None else None
        is_stale = age_days is not None and age_days > STALE_AFTER_DAYS
        results.append((name, len(catalog), oldest, age_days, is_stale))
    return results


def run_pricing_update() -> int:
    any_stale = False
    for name, count, oldest, age_days, is_stale in catalog_freshness():
        if oldest is None:
            print(f"{name}: no built-in models.")
            continue
        any_stale = any_stale or is_stale
        flag = " (worth checking for a newer release)" if is_stale else ""
        print(
            f"{name}: {count} model(s), oldest verified_date {oldest.isoformat()} "
            f"({age_days} days ago){flag}"
        )
    print()
    print("Inferrail never fetches pricing over the network. Every built-in price")
    print("is verified by hand against the vendor's own pricing page and shipped")
    print("in the package. To get newer verified prices:")
    print("  pip install --upgrade inferrail")
    print("Or declare your own verified price in inferrail.yaml's 'pricing:'")
    print("section (see inferrail.example.yaml) — 'source' and 'verified_date'")
    print("are required, same discipline as the built-in catalogs.")
    return 1 if any_stale else 0
