"""Opt-in provider settings for running Inferrail in front of another
OpenAI-compatible gateway — see docs/adr/0022's coexistence note.

- `price_as` asserts that an OpenAI-compatible upstream bills at a
  vendor's list prices, so the built-in catalog applies. The receipt's
  pricing source says so; it's the operator's assertion, not verification.
- `request_stream_usage` asks the upstream for the final usage chunk on
  streams, as Inferrail already does for verified OpenAI.
"""

from __future__ import annotations

import json

import httpx
import pytest

from inferrail.config.models import InferrailConfig, ProviderConfig
from inferrail.pricing.resolver import PricingResolver
from inferrail.providers.base import ChatMessage, NormalizedChatRequest
from inferrail.providers.registry import build_providers


def _providers(**litellm: object) -> dict[str, ProviderConfig]:
    return {
        "litellm": ProviderConfig.model_validate(
            {"type": "openai_compatible", "api_key_env": "K", "base_url": "http://gw/v1", **litellm}
        )
    }


def test_compatible_upstream_has_no_price_by_default() -> None:
    assert PricingResolver(_providers(), {}).resolve("litellm", "gpt-4o-mini") is None


def test_price_as_openai_applies_the_openai_catalog() -> None:
    price = PricingResolver(_providers(price_as="openai"), {}).resolve("litellm", "gpt-4o-mini")

    assert price is not None
    assert "price_as" in price.source and "litellm" in price.source


def test_price_as_matches_a_vendor_prefixed_model_name() -> None:
    resolver = PricingResolver(_providers(price_as="openai"), {})

    assert resolver.resolve("litellm", "openai/gpt-4o-mini") is not None
    assert resolver.resolve("litellm", "openai:gpt-4o-mini") is not None
    # A different vendor's prefix is never priced from the OpenAI catalog.
    assert resolver.resolve("litellm", "anthropic/gpt-4o-mini") is None


def test_price_as_unknown_model_stays_unpriced() -> None:
    assert PricingResolver(_providers(price_as="openai"), {}).resolve("litellm", "mystery") is None


def test_explicit_override_still_wins_over_price_as() -> None:
    config = InferrailConfig.model_validate(
        {
            "providers": {"litellm": {"type": "openai_compatible", "api_key_env": "K",
                                      "base_url": "http://gw/v1", "price_as": "openai"}},
            "routes": {"default": {"provider": "litellm", "model": "gpt-4o-mini"}},
            "pricing": {"litellm": {"gpt-4o-mini": {
                "input_usd_per_million": "9", "output_usd_per_million": "9",
                "source": "negotiated", "verified_date": "2026-09-29"}}},
        }
    )
    price = PricingResolver(config.providers, config.pricing).resolve("litellm", "gpt-4o-mini")

    assert price is not None and price.source == "negotiated"


def test_price_as_is_only_allowed_on_compatible_providers() -> None:
    with pytest.raises(ValueError):
        ProviderConfig.model_validate({"type": "openai", "api_key_env": "K", "price_as": "openai"})


@pytest.mark.parametrize(("flag", "expected"), [(True, {"include_usage": True}), (False, None)])
async def test_request_stream_usage_flag(
    monkeypatch: pytest.MonkeyPatch, flag: bool, expected: object
) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    config = InferrailConfig.model_validate(
        {
            "providers": {"litellm": {"type": "openai_compatible", "api_key_env": "K",
                                      "base_url": "http://gw/v1", "request_stream_usage": flag}},
            "routes": {"default": {"provider": "litellm", "model": "gpt-4o-mini"}},
        }
    )
    monkeypatch.setenv("K", "key")
    provider = build_providers(config)["litellm"]
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[attr-defined]

    request = NormalizedChatRequest(
        model="gpt-4o-mini", messages=[ChatMessage(role="user", content="hi")]
    )
    [_ async for _ in provider.stream(request, timeout=5)]

    assert seen[0].get("stream_options") == expected
