from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from inferrail.config.models import PriceEntry, ProviderConfig
from inferrail.pricing.builtin import BUILTIN_OPENAI_PRICING
from inferrail.pricing.builtin_anthropic import BUILTIN_ANTHROPIC_PRICING
from inferrail.pricing.resolver import PricingResolver
from inferrail.receipts.builder import build_receipt
from inferrail.receipts.calculator import CacheTokens, calculate_cost_usd


def _fixture_price(input_price: str = "1.00", output_price: str = "2.00") -> PriceEntry:
    # Unmistakably a test fixture, not a real verified price — see
    # docs/adr/0005-privacy-preserving-economic-receipts.md.
    return PriceEntry(
        input_usd_per_million=Decimal(input_price),
        output_usd_per_million=Decimal(output_price),
        source="test-fixture",
        verified_date=date(2020, 1, 1),
    )


def test_builtin_pricing_applies_to_real_openai_provider() -> None:
    providers = {"openai": ProviderConfig(type="openai", api_key_env="KEY")}
    resolver = PricingResolver(providers, overrides={})

    price = resolver.resolve("openai", "gpt-4o-mini")

    assert price is not None
    assert price.input_usd_per_million == Decimal("0.15")
    assert price.output_usd_per_million == Decimal("0.60")
    assert price.source
    assert price.verified_date is not None


def test_context_tiered_models_price_the_full_request_at_the_long_rate() -> None:
    # OpenAI bills a gpt-5.6 request above 272K input tokens at the long
    # rates for the *full* request, not only for the tokens past 272K.
    providers = {"openai": ProviderConfig(type="openai", api_key_env="KEY")}
    resolver = PricingResolver(providers, overrides={})

    terra = resolver.resolve("openai", "gpt-5.6-terra")
    assert terra is not None and terra.long_context is not None
    assert terra.long_context.above_input_tokens == 272_000

    short = calculate_cost_usd(272_000, 1_000, terra)
    assert short == Decimal("0.556000")  # 272k * $2 + 1k * $12, per 1M
    long = calculate_cost_usd(272_001, 1_000, terra)
    assert long == Decimal("1.106004")  # 272,001 * $4 + 1k * $18, per 1M

    for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        price = BUILTIN_OPENAI_PRICING[model]
        assert price.long_context is not None, model
        assert price.long_context.input_usd_per_million == 2 * price.input_usd_per_million
        assert price.long_context.output_usd_per_million == (
            price.output_usd_per_million * Decimal("1.5")
        )


def test_long_context_tier_prices_cached_input_and_the_receipt_records_the_tier() -> None:
    providers = {"openai": ProviderConfig(type="openai", api_key_env="KEY")}
    resolver = PricingResolver(providers, overrides={})
    cache = CacheTokens(creation=0, creation_5m=None, creation_1h=None, read=200_000)

    receipt = build_receipt(
        receipt_id="r",
        request_id="q",
        route="passthrough",
        provider="openai",
        model="gpt-5.6-luna",
        status="success",
        prompt_tokens=300_000,
        completion_tokens=10_000,
        pricing_resolver=resolver,
        attributes={},
        total_latency_ms=1.0,
        retry_count=0,
        cache=cache,
    )

    # 100k uncached * $0.40 + 200k cached * $0.04 + 10k out * $1.80, per 1M
    assert receipt.estimated_cost_usd == Decimal("0.066000")
    assert receipt.pricing is not None
    assert receipt.pricing.input_usd_per_million == Decimal("0.40")
    assert receipt.pricing.long_context is None


def test_long_context_price_entry_rejects_incomplete_rates() -> None:
    tier = {"above_input_tokens": 1000, "input_usd_per_million": "2", "output_usd_per_million": "4"}
    base = {
        "input_usd_per_million": "1",
        "output_usd_per_million": "2",
        "source": "https://example.com",
        "verified_date": "2026-10-06",
    }
    with pytest.raises(ValidationError, match="on both the base price"):
        PriceEntry(**base, cache_read_usd_per_million="0.1", long_context=tier)
    with pytest.raises(ValidationError, match="cache write rates"):
        PriceEntry(**base, cache_write_5m_usd_per_million="1.25", long_context=tier)
    assert PriceEntry(**base, long_context=tier).for_input_tokens(1001).input_usd_per_million == 2


def test_every_builtin_price_carries_verifiable_provenance() -> None:
    for model, price in BUILTIN_OPENAI_PRICING.items():
        assert price.source.startswith("https://"), model
        assert price.input_usd_per_million > 0, model
        assert price.output_usd_per_million > 0, model


def test_builtin_pricing_does_not_apply_to_openai_compatible_provider() -> None:
    # A same-shaped self-hosted endpoint could be serving a completely
    # different, differently-priced model under a colliding name — never
    # guess OpenAI's price applies to it.
    providers = {
        "local": ProviderConfig(
            type="openai_compatible", api_key_env="KEY", base_url="http://localhost:8001/v1"
        )
    }
    resolver = PricingResolver(providers, overrides={})

    assert resolver.resolve("local", "gpt-4o-mini") is None


def test_builtin_pricing_does_not_apply_when_base_url_overridden() -> None:
    # type: openai but pointed somewhere other than api.openai.com — not
    # verifiably OpenAI's real pricing anymore.
    providers = {
        "openai": ProviderConfig(
            type="openai", api_key_env="KEY", base_url="https://my-proxy.example.com/v1"
        )
    }
    resolver = PricingResolver(providers, overrides={})

    assert resolver.resolve("openai", "gpt-4o-mini") is None


def test_unknown_model_is_none_not_zero() -> None:
    providers = {"openai": ProviderConfig(type="openai", api_key_env="KEY")}
    resolver = PricingResolver(providers, overrides={})

    price = resolver.resolve("openai", "some-model-not-in-the-catalog")

    assert price is None


def test_unknown_provider_is_none() -> None:
    resolver = PricingResolver({}, overrides={})

    assert resolver.resolve("nope", "gpt-4o-mini") is None


def test_operator_override_wins_over_builtin() -> None:
    providers = {"openai": ProviderConfig(type="openai", api_key_env="KEY")}
    override = _fixture_price("9.99", "8.88")
    resolver = PricingResolver(providers, overrides={"openai": {"gpt-4o-mini": override}})

    price = resolver.resolve("openai", "gpt-4o-mini")

    assert price == override


def test_operator_override_applies_even_for_openai_compatible_provider() -> None:
    # The operator has explicitly declared this price — full control,
    # regardless of the built-in eligibility guard rail.
    providers = {
        "local": ProviderConfig(
            type="openai_compatible", api_key_env="KEY", base_url="http://localhost:8001/v1"
        )
    }
    override = _fixture_price()
    resolver = PricingResolver(providers, overrides={"local": {"my-model": override}})

    assert resolver.resolve("local", "my-model") == override


def test_price_entry_requires_source_and_verified_date() -> None:
    with pytest.raises(ValidationError):
        PriceEntry(input_usd_per_million=Decimal("1"), output_usd_per_million=Decimal("2"))  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Anthropic catalog (docs/adr/0014-anthropic-messages-passthrough.md)
# ---------------------------------------------------------------------------


def test_builtin_pricing_applies_to_real_anthropic_provider() -> None:
    providers = {"anthropic": ProviderConfig(type="anthropic", api_key_env="KEY")}
    resolver = PricingResolver(providers, overrides={})

    price = resolver.resolve("anthropic", "claude-sonnet-5")

    assert price is not None
    assert price.input_usd_per_million == Decimal("2.00")
    assert price.output_usd_per_million == Decimal("10.00")


def test_every_builtin_anthropic_price_carries_verifiable_provenance() -> None:
    for model, price in BUILTIN_ANTHROPIC_PRICING.items():
        assert price.source.startswith("https://"), model
        assert price.input_usd_per_million > 0, model
        assert price.output_usd_per_million > 0, model


def test_builtin_anthropic_pricing_does_not_apply_to_anthropic_compatible_provider() -> None:
    providers = {
        "local": ProviderConfig(
            type="anthropic_compatible", api_key_env="KEY", base_url="http://localhost:8002/v1"
        )
    }
    resolver = PricingResolver(providers, overrides={})

    assert resolver.resolve("local", "claude-sonnet-5") is None


def test_builtin_anthropic_pricing_does_not_apply_when_base_url_overridden() -> None:
    providers = {
        "anthropic": ProviderConfig(
            type="anthropic", api_key_env="KEY", base_url="https://my-proxy.example.com/v1"
        )
    }
    resolver = PricingResolver(providers, overrides={})

    assert resolver.resolve("anthropic", "claude-sonnet-5") is None


def test_builtin_catalogs_never_cross_contaminate_between_providers() -> None:
    # An anthropic-typed provider must never resolve an OpenAI model name
    # (or vice versa) just because both catalogs are consulted generically.
    providers = {"anthropic": ProviderConfig(type="anthropic", api_key_env="KEY")}
    resolver = PricingResolver(providers, overrides={})

    assert resolver.resolve("anthropic", "gpt-4o-mini") is None


def test_operator_override_wins_over_anthropic_builtin() -> None:
    providers = {"anthropic": ProviderConfig(type="anthropic", api_key_env="KEY")}
    override = _fixture_price("9.99", "8.88")
    resolver = PricingResolver(providers, overrides={"anthropic": {"claude-sonnet-5": override}})

    assert resolver.resolve("anthropic", "claude-sonnet-5") == override
