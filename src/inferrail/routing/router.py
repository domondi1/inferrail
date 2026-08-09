"""Routing: mapping a request to a provider + model.

v0.1 routing is deliberately static: a route name (carried in the request's
``model`` field, e.g. ``{"model": "default", ...}``) is looked up directly
in ``inferrail.yaml``. There is no cost/latency-aware or capability-aware
selection yet.

The ``RoutingContext`` / ``RoutingDecision`` split exists so that later,
richer routing signals (allowed providers, cost ceiling, latency budget,
historical reliability, ...) can be added to ``RoutingContext`` and a
``RoutingPolicy`` can replace this static lookup, without changing the
execution engine that consumes a ``RoutingDecision``.
"""

from __future__ import annotations

from dataclasses import dataclass

from inferrail.config.models import RouteConfig
from inferrail.errors import RoutingError


@dataclass(frozen=True)
class RoutingContext:
    """Everything known about a request at routing time."""

    requested_route: str


@dataclass(frozen=True)
class RoutingDecision:
    """The outcome of routing: exactly what to execute, and how."""

    route_name: str
    provider_name: str
    model: str
    max_retries: int
    timeout_seconds: float


class Router:
    """Resolves a :class:`RoutingContext` to a :class:`RoutingDecision`.

    Static/deterministic by design for v0.1 — see module docstring.
    """

    def __init__(self, routes: dict[str, RouteConfig]) -> None:
        self._routes = routes

    def resolve(self, context: RoutingContext) -> RoutingDecision:
        route = self._routes.get(context.requested_route)
        if route is None:
            raise RoutingError(
                f"no route named '{context.requested_route}' is configured; "
                f"known routes: {sorted(self._routes)}"
            )
        return RoutingDecision(
            route_name=context.requested_route,
            provider_name=route.provider,
            model=route.model,
            max_retries=route.max_retries,
            timeout_seconds=route.timeout_seconds,
        )
