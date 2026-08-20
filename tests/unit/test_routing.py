from __future__ import annotations

import pytest

from inferrail.config.models import RouteConfig
from inferrail.errors import RoutingError
from inferrail.routing.router import PASSTHROUGH_ROUTE_NAME, Router, RoutingContext


@pytest.fixture
def router() -> Router:
    return Router(
        {
            "default": RouteConfig(provider="openai", model="gpt-4o-mini"),
            "fast": RouteConfig(
                provider="openai", model="gpt-4o-mini", max_retries=2, timeout_seconds=5
            ),
        }
    )


def test_resolve_known_route(router: Router) -> None:
    decision = router.resolve(RoutingContext(requested_route="default"))

    assert decision.route_name == "default"
    assert decision.provider_name == "openai"
    assert decision.model == "gpt-4o-mini"
    assert decision.max_retries == 0
    assert decision.timeout_seconds == 30.0


def test_resolve_route_with_overrides(router: Router) -> None:
    decision = router.resolve(RoutingContext(requested_route="fast"))

    assert decision.max_retries == 2
    assert decision.timeout_seconds == 5


def test_resolve_unknown_route_raises(router: Router) -> None:
    with pytest.raises(RoutingError, match="no route named 'nope'"):
        router.resolve(RoutingContext(requested_route="nope"))


def test_resolve_unmatched_model_without_default_provider_still_raises(router: Router) -> None:
    """No `default_provider` configured: unchanged v0.1 strict behavior."""
    with pytest.raises(RoutingError, match="no route named 'gpt-5.6-sol'"):
        router.resolve(RoutingContext(requested_route="gpt-5.6-sol"))


def test_resolve_unmatched_model_passes_through_to_default_provider() -> None:
    passthrough_router = Router(
        {"default": RouteConfig(provider="openai", model="gpt-4o-mini")},
        default_provider="openai",
    )

    decision = passthrough_router.resolve(RoutingContext(requested_route="gpt-5.6-sol"))

    assert decision.route_name == PASSTHROUGH_ROUTE_NAME
    assert decision.provider_name == "openai"
    # The exact model the caller asked for, forwarded unchanged — not
    # translated, not validated against any known-model list.
    assert decision.model == "gpt-5.6-sol"
    assert decision.max_retries == 0
    assert decision.timeout_seconds == 30.0


def test_named_route_takes_priority_over_passthrough() -> None:
    """An operator can shadow a real model name with a route of the same
    name (e.g. to force nonstandard retry/timeout behavior for it), and
    that route wins over forwarding it as a bare passthrough."""
    shadowing_router = Router(
        {"gpt-5.6-sol": RouteConfig(provider="openai", model="gpt-5.6-sol", max_retries=3)},
        default_provider="openai",
    )

    decision = shadowing_router.resolve(RoutingContext(requested_route="gpt-5.6-sol"))

    assert decision.route_name == "gpt-5.6-sol"
    assert decision.max_retries == 3
