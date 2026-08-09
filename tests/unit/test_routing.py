from __future__ import annotations

import pytest

from inferrail.config.models import RouteConfig
from inferrail.errors import RoutingError
from inferrail.routing.router import Router, RoutingContext


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
