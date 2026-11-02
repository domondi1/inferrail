"""Prices read from the vendors' own pricing pages (markdown), the versioned snapshot, and how
the resolver uses it. Fixture pages only; nothing is fetched."""

# ruff: noqa: E501  (the fixtures reproduce the vendors' markdown tables, one row per line)

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal as D

from inferrail.config.models import PriceEntry, ProviderConfig
from inferrail.pricing import vendor_pages as vp
from inferrail.pricing.builtin import BUILTIN_OPENAI_PRICING
from inferrail.pricing.resolver import PricingResolver

DAY = date(2026, 11, 2)

OPENAI = """# Pricing
### Standard pricing data

| Model | Short context input | Short context output | Short context cached input | Short context 30m write | Short context 12h write | Long context input | Long context output | Long context cached input | Long context 30m write | Long context 12h write |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-next | $2.00 | $10.00 | $0.20 | $2.50 | $5.00 | $4.00 | $15.00 | $0.40 | $5.00 | $10.00 |
| gpt-tiered (<272K context length) | $2.50 | $15.00 | $0.25 | - | - | $5.00 | $22.50 | $0.50 | - | - |
| gpt-tiered-writes (<200K context length) | $1.00 | $5.00 | $0.10 | $1.25 | $2.50 | $2.00 | $7.50 | $0.20 | $2.50 | $5.00 |

| Model | Input | Output | Cached input |
| --- | --- | --- | --- |
| gpt-flat-nano | $0.20 | $1.25 | $0.02 |
| gpt-flat-pro | $30.00 | $180.00 | - |
| gpt-misread | $5.00 | $0.10 | $0.01 |

### Batch pricing data

| Model | Input | Output | Cached input |
| --- | --- | --- | --- |
| gpt-flat-nano | $0.10 | $0.60 | $0.01 |
"""

ANTHROPIC = """## Model pricing

| Model | Base input tokens | 5m cache writes | 1h cache writes | Cache hits and refreshes | Output tokens |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Claude Sonnet 7.1 | $2 / MTok | $2.50 / MTok | $4 / MTok | $0.10 / MTok<sup>2</sup> | $10 / MTok |
| Claude Opus 4.6 | $5 / MTok | $6.25 / MTok | $10 / MTok | $0.50 / MTok | $25 / MTok |
| Claude Sonnet 4.5 | $3 / MTok | $3.75 / MTok | $6 / MTok | $0.30 / MTok | $15 / MTok |
| Claude Opus 4.1 ([retired, except on Bedrock](https://example.com)) | $15 / MTok | $18.75 / MTok | $30 / MTok | $1.50 / MTok | $75 / MTok |
| Claude Haiku 7.5 (for prompts up to 100,000 tokens) | $0.10 / MTok | $0.125 / MTok | $0.20 / MTok | $0.01 / MTok | $0.50 / MTok |
| Claude Haiku 7.5 (for prompts over 100,000 tokens) | $0.50 / MTok | $0.625 / MTok | $1 / MTok | $0.05 / MTok | $2.50 / MTok |

## Batch processing

### Long context pricing

Claude 4.6 and later models (except Claude Haiku 7.5) include the full [1M token context window](https://example.com) at standard pricing.
"""


def test_openai_flat_and_tiered_rows_are_read_exactly():
    p = vp.parse_openai(OPENAI, DAY)
    nano = p.prices["gpt-flat-nano"]  # the standard table, not the batch one below it
    assert (nano.input_usd_per_million, nano.output_usd_per_million,
            nano.cache_read_usd_per_million) == (D("0.20"), D("1.25"), D("0.02"))
    assert nano.source == vp.OPENAI_PRICING_PAGE and nano.verified_date == DAY
    assert p.prices["gpt-flat-pro"].cache_read_usd_per_million is None
    tiered = p.prices["gpt-tiered"]
    assert tiered.long_context is not None
    assert tiered.long_context.above_input_tokens == 272_000
    assert tiered.long_context.output_usd_per_million == D("22.50")


def test_openai_rows_that_cant_be_priced_exactly_stay_unpriced():
    p = vp.parse_openai(OPENAI, DAY)
    assert "gpt-next" not in p.prices
    assert p.skipped["gpt-next"] == "long-context threshold not stated on the pricing page"
    assert p.skipped["gpt-tiered-writes"] == "long-context cache-write rates not representable yet"
    assert p.skipped["gpt-misread"] == "failed sanity checks"


def test_anthropic_flat_pricing_rule_is_read_from_the_page():
    p = vp.parse_anthropic(ANTHROPIC, DAY)
    assert set(p.prices) == {"claude-sonnet-7-1", "claude-opus-4-6"}
    s = p.prices["claude-sonnet-7-1"]
    assert (s.cache_write_5m_usd_per_million, s.cache_write_1h_usd_per_million,
            s.cache_read_usd_per_million) == (D("2.50"), D("4"), D("0.10"))
    assert p.skipped["claude-sonnet-4-5"].startswith("page doesn't state flat pricing")
    assert p.skipped["claude-opus-4-1"] == "retired"
    assert p.skipped["claude-haiku-7-5"].startswith("priced by prompt length")


def test_without_the_flat_pricing_statement_no_anthropic_model_is_priced():
    page = ANTHROPIC.split("### Long context pricing")[0]
    p = vp.parse_anthropic(page, DAY)
    assert p.prices == {}
    assert set(p.skipped) >= {"claude-sonnet-7-1", "claude-opus-4-6"}


def test_the_snapshot_round_trips_and_drops_invalid_entries(tmp_path, monkeypatch):
    parsed = {"openai": vp.parse_openai(OPENAI, DAY), "anthropic": vp.parse_anthropic(ANTHROPIC, DAY)}
    doc = vp.snapshot_document(parsed, {"openai": "a" * 64, "anthropic": "b" * 64}, DAY)
    doc["prices"]["openai"]["gpt-broken"] = {"input_usd_per_million": "-1"}  # type: ignore[index]
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / vp.SNAPSHOT_FILE).write_text(json.dumps(doc))
    monkeypatch.setattr(vp, "__package__", None)

    import importlib.resources as resources

    monkeypatch.setattr(resources, "files", lambda _pkg: pkg)
    loaded = vp.load_snapshot()
    assert "gpt-broken" not in loaded["openai"]
    assert loaded["openai"]["gpt-flat-nano"] == parsed["openai"].prices["gpt-flat-nano"]
    assert doc["unpriced"]["openai"]["gpt-next"]  # type: ignore[index]


def _resolver() -> PricingResolver:
    return PricingResolver(
        providers={"openai": ProviderConfig(type="openai", api_key_env="X"),
                   "anthropic": ProviderConfig(type="anthropic", api_key_env="Y")},
        overrides={},
    )


def test_the_packaged_snapshot_prices_models_from_the_vendor_page():
    r = _resolver()
    nano = r.resolve("openai", "gpt-5.4-nano")
    assert nano is not None and nano.source == vp.OPENAI_PRICING_PAGE
    # Entries the snapshot can't express keep their hand-verified price (context tiers).
    terra = r.resolve("openai", "gpt-5.6-terra")
    assert terra == BUILTIN_OPENAI_PRICING["gpt-5.6-terra"] and terra.long_context is not None
    # Never a guess: a model on no page and in no catalog stays unpriced.
    assert r.resolve("openai", "gpt-unknown") is None
    assert r.resolve("anthropic", "claude-haiku-5-5") is None


def test_every_packaged_snapshot_entry_is_valid_and_sourced():
    for vendor, models in vp.load_snapshot().items():
        assert models, vendor
        for model, entry in models.items():
            assert isinstance(entry, PriceEntry), model
            assert entry.source in (vp.OPENAI_PRICING_PAGE, vp.ANTHROPIC_PRICING_PAGE), model
            if entry.cache_read_usd_per_million is not None:
                assert entry.cache_read_usd_per_million < entry.input_usd_per_million, model


def test_a_catalog_change_never_rewrites_a_recorded_price():
    """Receipts embed a copy of the PriceEntry used; a later snapshot is a different object."""
    before = _resolver().resolve("openai", "gpt-5.4-nano")
    assert before is not None
    recorded = before.model_dump(mode="json")
    changed = before.model_copy(update={"input_usd_per_million": D("0.30")})
    assert recorded["input_usd_per_million"] == "0.20" and changed.input_usd_per_million == D("0.30")
    assert PriceEntry.model_validate(recorded).input_usd_per_million == D("0.20")
