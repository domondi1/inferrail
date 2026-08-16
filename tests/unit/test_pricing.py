from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from inferrail.config.models import PriceEntry, ProviderConfig
from inferrail.pricing.resolver import PricingResolver


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
