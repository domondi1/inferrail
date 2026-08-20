"""Routing: mapping a request to a provider + model.

v0.1 routing is deliberately static: a route name (carried in the request's
``model`` field, e.g. ``{"model": "default", ...}``) is looked up directly
in ``inferrail.yaml``. There is no cost/latency-aware or capability-aware
selection yet.

If the request's ``model`` doesn't match any named route, and a
``default_provider`` is configured, the request is not rejected — it's
forwarded to that provider with ``model`` passed through unchanged. This
lets an application send whatever upstream model id it wants
(``gpt-5.6-sol``, a model that doesn't exist yet) without Inferrail needing
a pre-registered route for it. See
docs/adr/0007-model-passthrough-routing.md. Named routes still take
priority — an operator can shadow a real model name with a route of the
same name (e.g. to force nonstandard retry/timeout behavior for it), and
that route wins.

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

# Sentinel `RoutingDecision.route_name` for a passthrough request — one that
# matched no named route and was forwarded via `default_provider` instead.
# Distinct from any real route name so `inferrail report --by route` can
# still tell "used a configured route" apart from "passed through"; `--by
# model` already gives the per-upstream-model breakdown within that bucket.
PASSTHROUGH_ROUTE_NAME = "passthrough"

_DEFAULT_MAX_RETRIES: int = RouteConfig.model_fields["max_retries"].default
_DEFAULT_TIMEOUT_SECONDS: float = RouteConfig.model_fields["timeout_seconds"].default


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

    def __init__(
        self, routes: dict[str, RouteConfig], default_provider: str | None = None
    ) -> None:
        self._routes = routes
        self._default_provider = default_provider

    def resolve(self, context: RoutingContext) -> RoutingDecision:
        route = self._routes.get(context.requested_route)
        if route is not None:
            return RoutingDecision(
                route_name=context.requested_route,
                provider_name=route.provider,
                model=route.model,
                max_retries=route.max_retries,
                timeout_seconds=route.timeout_seconds,
            )

        if self._default_provider is not None:
            return RoutingDecision(
                route_name=PASSTHROUGH_ROUTE_NAME,
                provider_name=self._default_provider,
                model=context.requested_route,
                max_retries=_DEFAULT_MAX_RETRIES,
                timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
            )

        raise RoutingError(
            f"no route named '{context.requested_route}' is configured; "
            f"known routes: {sorted(self._routes)}"
        )
