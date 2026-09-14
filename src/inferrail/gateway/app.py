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
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from inferrail import __version__
from inferrail.budgets.enforcement import BudgetEnforcer
from inferrail.budgets.store import BudgetStore
from inferrail.config.models import InferrailConfig
from inferrail.dashboard import find_dashboard_dist
from inferrail.errors import (
    AuthenticationError,
    BudgetExceededError,
    ConfigurationError,
    GatewayAuthenticationError,
    InferrailError,
    InvalidRequestError,
    LocalApiAuthenticationError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    RoutingError,
    UnsupportedFeatureError,
)
from inferrail.errors.codes import code_for, docs_url_for
from inferrail.gateway.anthropic_execution import AnthropicInferenceEngine
from inferrail.gateway.execution import InferenceEngine
from inferrail.gateway.routes import router as api_router
from inferrail.gateway.schemas import ErrorDetail, ErrorResponse
from inferrail.localapi.routes import router as local_api_router
from inferrail.localapi.token import ensure_local_api_token
from inferrail.pricing.resolver import PricingResolver
from inferrail.providers.anthropic_base import AnthropicMessagesProvider
from inferrail.providers.base import Provider
from inferrail.providers.registry import build_anthropic_providers, build_providers
from inferrail.receipts.sinks import build_receipt_sink
from inferrail.receipts.sqlite_store import ReceiptsStore
from inferrail.routing.router import Router
from inferrail.telemetry.sinks import build_telemetry_sink

_logger = logging.getLogger("inferrail.gateway")

# Checked in order; first match wins. Deliberately explicit rather than a
# generic "does the error have a status_code" duck-type, so adding a new
# InferrailError subclass forces a conscious choice of HTTP status here.
_STATUS_BY_ERROR: list[tuple[type[InferrailError], int]] = [
    (GatewayAuthenticationError, 401),
    (LocalApiAuthenticationError, 401),
    (AuthenticationError, 401),
    (BudgetExceededError, 402),
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


def _error_details(exc: InferrailError) -> dict[str, str] | None:
    """Structured, machine-readable fields for error types where
    `message` alone (free text, even if not upstream-tainted) isn't
    enough for a caller to programmatically react — currently just
    `BudgetExceededError` (see docs/PRODUCT.md's v0.3.0 acceptance
    criteria: "block responses are machine-readable")."""
    if isinstance(exc, BudgetExceededError):
        return {
            "budget_id": exc.budget_id,
            "scope": exc.scope,
            "scope_value": exc.scope_value or "",
            "window": exc.window,
            "mode": exc.mode,
            "limit_usd": str(exc.limit_usd),
            "spent_so_far_usd": str(exc.spent_so_far_usd),
            "estimated_request_usd": str(exc.estimated_request_usd),
            "projected_total_usd": str(exc.projected_total_usd),
        }
    return None


def create_app(
    config: InferrailConfig,
    *,
    app_mode: bool = False,
    local_outcomes_path: Path | None = None,
) -> FastAPI:
    """`app_mode` mounts the local control API (`/v1/local/*` — see
    docs/adr/0016-local-control-api.md), guarded by a mandatory
    per-install token. Only `inferrail serve --app-mode` sets this;
    every other caller of `create_app` (including every existing test)
    is completely unaffected — no new route, no new state, no new file
    touched on disk. When `app_mode` is true, `config` must already have
    `receipts.sink: sqlite` and `budgets.enabled: true` (the CLI's
    `--app-mode` setup guarantees both); `local_outcomes_path` is where
    `inferrail work outcome` records live for the `/v1/local/work*`
    routes to read back.
    """
    # require_keys=False: the server (and /health) must be able to start
    # even before a provider's secret is configured. A missing key only
    # becomes an error when a request actually reaches that provider — see
    # OpenAIProvider.complete. `inferrail config check` still validates
    # keys eagerly via build_providers' default.
    providers: dict[str, Provider] = build_providers(config, require_keys=False)
    anthropic_providers: dict[str, AnthropicMessagesProvider] = build_anthropic_providers(
        config, require_keys=False
    )
    # Shared across both wire-format pipelines (docs/adr/0014): routing,
    # pricing, receipts, and telemetry are all already provider/format-
    # agnostic — one routes: section and one ledger serve /v1/chat/completions
    # and /v1/messages alike.
    router = Router(config.routes, default_provider=config.default_provider)
    telemetry = build_telemetry_sink(config.telemetry)
    pricing_resolver = PricingResolver(config.providers, config.pricing)
    receipts = build_receipt_sink(config.receipts)
    # Only touch the budgets store at all when enabled — config validation
    # (InferrailConfig._budgets_require_sqlite_receipts) already guarantees
    # `receipts` is a real ReceiptsStore whenever this is true, so the
    # enforcer never has to defend against a mismatched sink at request
    # time. See docs/adr/0015-budget-enforcement.md.
    budget_enforcer: BudgetEnforcer | None = None
    budget_store: BudgetStore | None = None
    if config.budgets.enabled:
        assert isinstance(receipts, ReceiptsStore)  # guaranteed by config validation above
        budget_store = BudgetStore(config.budgets.path)
        budget_enforcer = BudgetEnforcer(budget_store, receipts, pricing_resolver)
    engine = InferenceEngine(
        router, providers, telemetry, pricing_resolver, receipts, budgets=budget_enforcer
    )
    anthropic_engine = AnthropicInferenceEngine(
        router, anthropic_providers, telemetry, pricing_resolver, receipts,
        budgets=budget_enforcer,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        for provider in (*providers.values(), *anthropic_providers.values()):
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()

    app = FastAPI(title="Inferrail", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.engine = engine
    app.state.anthropic_engine = anthropic_engine
    # Optional shared-secret gateway auth: unset by default (localhost dev
    # mode). If set, gateway/routes.py rejects requests to inference
    # endpoints that don't present a matching bearer token. See
    # docs/PRODUCT.md's security section for why this exists.
    app.state.gateway_token = os.environ.get("INFERRAIL_GATEWAY_TOKEN") or None
    app.include_router(api_router)

    if app_mode:
        if not isinstance(receipts, ReceiptsStore) or budget_store is None:
            raise ConfigurationError(
                "--app-mode requires receipts.sink: sqlite and budgets.enabled: true — "
                "the CLI's own --app-mode setup should have guaranteed both; see "
                "docs/adr/0016-local-control-api.md"
            )
        app_data = Path(config.budgets.path).parent
        app.state.local_api_token = ensure_local_api_token(app_data / "local-api-token")
        app.state.local_receipts_store = receipts
        app.state.local_budget_store = budget_store
        app.state.local_outcomes_path = local_outcomes_path or (app_data / "work-outcomes.jsonl")
        app.include_router(local_api_router)

        # The dashboard (docs/adr/0017) is a static SPA -- mounted only
        # when a build is actually found; a missing/unbuilt dashboard is
        # not an error (see find_dashboard_dist's docstring). `html=True`
        # serves index.html for `/dashboard` and `/dashboard/`; no SPA
        # catch-all route is needed because the dashboard uses hash-based
        # routing exclusively (the server never sees `#/...`).
        dashboard_dist = find_dashboard_dist()
        app.state.dashboard_dist = dashboard_dist
        if dashboard_dist is not None:
            app.mount(
                "/dashboard", StaticFiles(directory=dashboard_dist, html=True), name="dashboard"
            )

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
        error_code = code_for(exc)
        body = ErrorResponse(
            error=ErrorDetail(
                message=str(exc),
                type=type(exc).__name__,
                code=error_code.code,
                remediation=error_code.remediation,
                docs_url=docs_url_for(error_code.code),
                details=_error_details(exc),
            )
        )
        return JSONResponse(status_code=status, content=body.model_dump())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        req: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # A field ChatCompletionRequest doesn't model at all (extra="forbid"
        # — see gateway/schemas.py) is a meaningfully unsupported parameter
        # (response_format, seed, ...), not a generic malformed request —
        # promote it to Inferrail's normal ErrorResponse/error-code shape
        # instead of FastAPI's default 422 body, so it's just as
        # machine-readable as every other rejection. Any other validation
        # failure (missing/mistyped field) keeps FastAPI's default handling
        # unchanged.
        unsupported_fields = sorted(
            str(error["loc"][-1]) for error in exc.errors() if error["type"] == "extra_forbidden"
        )
        if not unsupported_fields:
            return await request_validation_exception_handler(req, exc)
        return await handle_inferrail_error(
            req,
            UnsupportedFeatureError(
                "request included field(s) Inferrail does not forward or transform: "
                f"{', '.join(unsupported_fields)} — see docs/PRODUCT.md for the exact "
                "supported request surface; unsupported fields are rejected, never "
                "silently ignored"
            ),
        )

    return app
