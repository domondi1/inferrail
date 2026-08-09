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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from inferrail import __version__
from inferrail.config.models import InferrailConfig
from inferrail.errors import (
    AuthenticationError,
    ConfigurationError,
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
from inferrail.providers.base import Provider
from inferrail.providers.registry import build_providers
from inferrail.routing.router import Router
from inferrail.telemetry.sinks import build_telemetry_sink

_logger = logging.getLogger("inferrail.gateway")

# Checked in order; first match wins. Deliberately explicit rather than a
# generic "does the error have a status_code" duck-type, so adding a new
# InferrailError subclass forces a conscious choice of HTTP status here.
_STATUS_BY_ERROR: list[tuple[type[InferrailError], int]] = [
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
    engine = InferenceEngine(router, providers, telemetry)

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
    app.include_router(api_router)

    @app.exception_handler(InferrailError)
    async def handle_inferrail_error(_: Request, exc: InferrailError) -> JSONResponse:
        status = _status_for(exc)
        _logger.warning("request failed with %s: %s", type(exc).__name__, exc)
        body = ErrorResponse(error=ErrorDetail(message=str(exc), type=type(exc).__name__))
        return JSONResponse(status_code=status, content=body.model_dump())

    return app
