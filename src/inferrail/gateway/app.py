"""FastAPI application factory.

`create_app` wires config -> providers -> router -> telemetry -> engine and
returns a plain `FastAPI` instance. No module-level global state: every
piece needed to serve a request is built here and attached to `app.state`,
which is what makes the gateway and engine testable in isolation (see
tests/unit/test_gateway.py) and safe to construct more than once in the
same process (e.g. in tests).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from inferrail import __version__
from inferrail.config.models import InferrailConfig
from inferrail.errors import (
    AuthenticationError,
    ConfigurationError,
    GatewayAuthenticationError,
    InferrailError,
    InvalidRequestError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    RoutingError,
    UnsupportedFeatureError,
)
from inferrail.gateway.execution import InferenceEngine
from inferrail.gateway.routes import router as api_router
from inferrail.gateway.schemas import ErrorDetail, ErrorResponse
from inferrail.pricing.resolver import PricingResolver
from inferrail.providers.base import Provider
from inferrail.providers.registry import build_providers
from inferrail.receipts.sinks import build_receipt_sink
from inferrail.routing.router import Router
from inferrail.telemetry.sinks import build_telemetry_sink

_logger = logging.getLogger("inferrail.gateway")

# Checked in order; first match wins. Deliberately explicit rather than a
# generic "does the error have a status_code" duck-type, so adding a new
# InferrailError subclass forces a conscious choice of HTTP status here.
_STATUS_BY_ERROR: list[tuple[type[InferrailError], int]] = [
    (GatewayAuthenticationError, 401),
    (AuthenticationError, 401),
    (RateLimitError, 429),
    (ProviderTimeoutError, 504),
    (InvalidRequestError, 400),
    (UnsupportedFeatureError, 400),
    (RoutingError, 400),
    (ConfigurationError, 500),
    (ProviderError, 502),
]


def _status_for(exc: InferrailError) -> int:
    for exc_type, status in _STATUS_BY_ERROR:
        if isinstance(exc, exc_type):
            return status
    return 500


def create_app(config: InferrailConfig) -> FastAPI:
    providers: dict[str, Provider] = build_providers(config)
    router = Router(config.routes)
    telemetry = build_telemetry_sink(config.telemetry)
    pricing_resolver = PricingResolver(config.providers, config.pricing)
    receipts = build_receipt_sink(config.receipts)
    engine = InferenceEngine(router, providers, telemetry, pricing_resolver, receipts)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        for provider in providers.values():
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()

    app = FastAPI(title="Inferrail", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.engine = engine
    # Optional shared-secret gateway auth: unset by default (localhost dev
    # mode). If set, gateway/routes.py rejects requests to inference
    # endpoints that don't present a matching bearer token. See
    # docs/PRODUCT.md's security section for why this exists.
    app.state.gateway_token = os.environ.get("INFERRAIL_GATEWAY_TOKEN") or None
    app.include_router(api_router)

    @app.exception_handler(InferrailError)
    async def handle_inferrail_error(_: Request, exc: InferrailError) -> JSONResponse:
        status = _status_for(exc)
        # Operator-facing log: must use the telemetry-safe summary, not
        # str(exc) — for a ProviderError, str(exc) may embed upstream,
        # provider-controlled free text (see ProviderError.safe_summary).
        # The caller-facing response body below is a separate case: it goes
        # back to the same caller whose content this is, so it may retain
        # full detail.
        _logger.warning("request failed with %s: %s", type(exc).__name__, exc.safe_summary)
        body = ErrorResponse(error=ErrorDetail(message=str(exc), type=type(exc).__name__))
        return JSONResponse(status_code=status, content=body.model_dump())

    return app
