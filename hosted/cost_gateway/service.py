"""Inferrail Cost Gateway -- hosted, self-serve, one-click trial service.

Lives outside `src/inferrail`, exactly like `hosted/work_economics`,
`hosted/a2a_economic_authority`, and `hosted/ap_exceptions` (see
docs/adr/0004, docs/adr/0010, docs/adr/0012). Authorized as a second,
parallel hosted product track under the private repo's D36 (Cost Gateway
ADR, Phase 1) -- it does not modify, and is not required by, the
self-hosted `inferrail serve` CLI path, or any other existing hosted
service.

**What this service does.** Any visitor, with no account, gets from
`POST /v1/trial` a short-lived, isolated trial tenant: a bearer token,
this instance's own `base_url`, and zero-key demo-mode traffic
immediately available (`POST /v1/demo/chat/completions`). They may
optionally submit their own OpenAI and/or Anthropic API key
(`POST /v1/trial/{tenant_id}/keys`) to proxy real traffic through their
own key (`POST /v1/chat/completions`, `POST /v1/messages`) and see a
real, payload-free receipt (`GET /v1/receipts`).

**What this service structurally cannot do.** A submitted provider key
is held in process memory only (`keys.py`) -- never written to disk,
never included in a receipt, never forwarded to any third party, and
used for nothing except proxying that same tenant's own requests to the
corresponding provider. Every request is authenticated
(`Authorization: Bearer <trial-api-key>`), isolated per tenant
(`tenant_store.py` -- two SQLite files per tenant, not a shared table),
rate-limited, budget-capped (a small default daily spend cap per
tenant), and subject to a per-request timeout.

**Trial lifetime, visible by design.** A trial starts with a 24 hour
demo-mode expiry. Submitting a real key shortens (never extends) that
expiry to at most 4 hours from the moment the key was submitted (see
`trial.py`). `GET /v1/trial/{tenant_id}` reports `seconds_remaining` on
every call specifically so a UI can render a live, impossible-to-miss
countdown -- per explicit founder instruction that trial deletion must be
highly visible, not a one-time notice.

**Threat model, restated (required every time key handling is touched):**
passive log/telemetry leakage, data-at-rest compromise, cross-tenant
leakage, and over-broad use are each mitigated structurally, not just by
policy -- see `keys.py`'s module docstring and this service's README for
the full write-up and the tests that verify each mitigation.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

from auth import RateLimiter, authenticate, rate_limiter_from_env
from demo_provider import (
    DEMO_MODEL_NAME,
    DEMO_PROVIDER_NAME,
    DemoProvider,
    demo_pricing_overrides,
)
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from keys import KeyVault
from pydantic import BaseModel
from tenant_store import DEFAULT_DAILY_BUDGET_USD, TenantStoreRegistry, TenantStores
from trial import TRIAL_NOTICE, Tenant, TrialRegistry, iso, trial_registry_from_env

from inferrail.budgets.enforcement import spent_so_far_usd
from inferrail.budgets.schema import new_budget_id
from inferrail.config.models import ProviderConfig, RouteConfig
from inferrail.errors import (
    AuthenticationError,
    BudgetExceededError,
    InferrailError,
    InvalidRequestError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    RoutingError,
    UnsupportedFeatureError,
)
from inferrail.errors.codes import code_for, docs_url_for
from inferrail.gateway.anthropic_execution import AnthropicInferenceEngine
from inferrail.gateway.anthropic_schemas import MessagesRequest, MessagesResponse
from inferrail.gateway.attribution import extract_attributes
from inferrail.gateway.execution import InferenceEngine
from inferrail.gateway.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorDetail,
    ErrorResponse,
)
from inferrail.pricing.resolver import PricingResolver
from inferrail.providers.anthropic import AnthropicProvider
from inferrail.providers.openai import OpenAIProvider
from inferrail.receipts.schema import InferenceReceipt
from inferrail.routing.router import Router
from inferrail.telemetry.sinks import NullTelemetrySink

REQUEST_TIMEOUT_SECONDS = float(os.environ.get("COST_GATEWAY_REQUEST_TIMEOUT_SECONDS", "120"))
"""Known Phase 1 limitation, documented rather than hidden: this wraps the
*entire* request/response cycle, including a streaming response's full
duration (see `timeout_middleware` below). A very long real completion
could be cut off. Raise via env var if that becomes a real problem before
this is revisited."""

MAX_REQUEST_BODY_BYTES = int(
    os.environ.get("COST_GATEWAY_MAX_REQUEST_BODY_BYTES", str(256 * 1024))
)
MAX_KEY_LENGTH = 4096
"""A plain length guard, checked in Python (never a pydantic field
constraint) so a too-long value is rejected without FastAPI's default
validation-error response ever echoing it back -- see `SubmitKeysRequest`
and `_validate_key_shape`."""


def _cors_origins_from_env() -> list[str]:
    """The Phase 2 website (tryinferrail.com, or a local static-file
    server while developing it) calls this API directly from a browser,
    which requires CORS. Defaults to `*` -- safe here specifically
    because every route that reads or changes anything is gated on a
    bearer token an attacker cannot obtain by getting a victim's browser
    to make a cross-origin request (there is no cookie-based or
    ambient-credential auth anywhere in this service for a permissive
    CORS policy to expose). Narrow via `COST_GATEWAY_CORS_ORIGINS`
    (comma-separated) for a production deployment that wants to restrict
    this to its own known frontend origin(s)."""
    raw = os.environ.get("COST_GATEWAY_CORS_ORIGINS", "*").strip()
    if raw == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]

_STATUS_BY_ERROR: list[tuple[type[InferrailError], int]] = [
    (AuthenticationError, 401),
    (BudgetExceededError, 402),
    (RateLimitError, 429),
    (ProviderTimeoutError, 504),
    (InvalidRequestError, 400),
    (UnsupportedFeatureError, 400),
    (RoutingError, 400),
    (ProviderError, 502),
]


def _status_for(exc: InferrailError) -> int:
    for exc_type, status in _STATUS_BY_ERROR:
        if isinstance(exc, exc_type):
            return status
    return 500


def _error_details(exc: InferrailError) -> dict[str, str] | None:
    """Mirrors `gateway/app.py`'s identical helper -- machine-readable
    fields for a budget block, the one error type where free text alone
    isn't enough for a caller to react to programmatically."""
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


def _shared_pricing_resolver() -> PricingResolver:
    """One `PricingResolver`, shared across every tenant -- it is pure
    and stateless (a function of provider *type*, never of a tenant's
    actual key), so sharing it introduces no cross-tenant coupling.
    Declares `openai`/`anthropic` provider *types* only (no real key, no
    `base_url` override) purely so `PricingResolver.resolve` recognizes
    them as verifiably-the-real-vendor's-API and applies the built-in
    catalog -- see that class's own docstring for why an overridden
    `base_url` would disable this. Also carries the demo provider's
    fixed `DEMO`-labeled price."""
    return PricingResolver(
        providers={
            "openai": ProviderConfig(type="openai", api_key_env="COST_GATEWAY_UNUSED_OPENAI"),
            "anthropic": ProviderConfig(
                type="anthropic", api_key_env="COST_GATEWAY_UNUSED_ANTHROPIC"
            ),
        },
        overrides=demo_pricing_overrides(),
    )


class SubmitKeysRequest(BaseModel):
    """Deliberately unconstrained beyond type (`str | None`, `extra:
    forbid`) -- see `MAX_KEY_LENGTH`'s docstring for why real validation
    happens in `_validate_key_shape` inside the route handler instead of
    as a pydantic field constraint."""

    model_config = {"extra": "forbid"}

    openai_key: str | None = None
    anthropic_key: str | None = None


def _validate_key_shape(value: str, *, field_name: str) -> str:
    """Raises a generic `HTTPException` whose message never includes
    `value` -- the one place a malformed key could otherwise leak into an
    HTTP response body. See `keys.py`'s threat-model docstring."""
    stripped = value.strip()
    if not stripped:
        raise HTTPException(status_code=422, detail=f"{field_name} must not be empty")
    if len(stripped) > MAX_KEY_LENGTH:
        raise HTTPException(
            status_code=422, detail=f"{field_name} exceeds the {MAX_KEY_LENGTH}-character limit"
        )
    if any(ch.isspace() for ch in stripped):
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must not contain whitespace -- check for a stray "
            "newline or space from copy-pasting",
        )
    return stripped


def _client_ip(request: Request) -> str:
    """Reflects the real client address when this process is run with
    `uvicorn.run(..., proxy_headers=True, forwarded_allow_ips="*")`
    behind a reverse proxy (e.g. Render) -- same reasoning as
    `hosted/ap_exceptions/service.py`'s identical helper."""
    return request.client.host if request.client is not None else "unknown"


def _base_url(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def _receipt_json(receipt: InferenceReceipt) -> dict[str, Any]:
    return {
        "receipt_id": receipt.receipt_id,
        "request_id": receipt.request_id,
        "timestamp": receipt.timestamp.isoformat(),
        "route": receipt.route,
        "provider": receipt.provider,
        "model": receipt.model,
        "status": receipt.status,
        "prompt_tokens": receipt.prompt_tokens,
        "completion_tokens": receipt.completion_tokens,
        "estimated_cost_usd": (
            str(receipt.estimated_cost_usd) if receipt.estimated_cost_usd is not None else None
        ),
        "attributes": receipt.attributes,
        "total_latency_ms": receipt.total_latency_ms,
        "retry_count": receipt.retry_count,
    }


async def _stream_and_close(inner: AsyncIterator[bytes], provider: Any) -> AsyncIterator[bytes]:
    """Wraps a provider-backed streaming response so the ephemeral,
    per-request `Provider` (holding the tenant's real key in its own
    `httpx.AsyncClient`) is always closed once the stream ends, is
    cancelled, or fails -- mirrors `gateway/execution.py`'s own
    `ctx.remaining.aclose()` discipline, applied here to the per-tenant
    provider this service constructs, which the shared engine code has
    no reason to know about."""
    try:
        async for chunk in inner:
            yield chunk
    finally:
        aclose = getattr(provider, "aclose", None)
        if aclose is not None:
            await aclose()


def create_app(data_dir: Path) -> FastAPI:
    app = FastAPI(title="Inferrail Cost Gateway", version="1")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins_from_env(),
        allow_methods=["GET", "POST", "DELETE"],
        # "*" rather than an exact allowlist: attribution headers are
        # caller-named (X-Inferrail-Attribute-<anything>, see
        # gateway.attribution), so a fixed header list can't cover them.
        # Safe for the same reason allow_origins=* is safe here -- see
        # `_cors_origins_from_env`'s docstring.
        allow_headers=["*"],
    )
    pricing_resolver = _shared_pricing_resolver()
    daily_budget_usd = Decimal(
        os.environ.get("COST_GATEWAY_DAILY_BUDGET_USD", str(DEFAULT_DAILY_BUDGET_USD))
    )
    tenant_stores = TenantStoreRegistry(
        data_dir, daily_budget_usd=daily_budget_usd, pricing_resolver=pricing_resolver
    )
    key_vault = KeyVault()
    limiter: RateLimiter = rate_limiter_from_env()

    def _on_purge(tenant_id: str) -> None:
        # Wipes both the tenant's storage AND any real key held for it --
        # a key must never outlive the tenant it was submitted for,
        # whether purged by the periodic sweep, a lazy purge on lookup,
        # or an explicit DELETE /v1/trial/{tenant_id}.
        tenant_stores.purge_tenant(tenant_id)
        key_vault.forget(tenant_id)

    trial_registry: TrialRegistry = trial_registry_from_env(on_purge=_on_purge)

    @app.middleware("http")
    async def timeout_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        try:
            return await asyncio.wait_for(call_next(request), timeout=REQUEST_TIMEOUT_SECONDS)
        except TimeoutError:
            return JSONResponse(
                status_code=504,
                content={"detail": f"request exceeded {REQUEST_TIMEOUT_SECONDS:.0f}s timeout"},
            )

    @app.middleware("http")
    async def request_size_limit_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Applies to every route, including the unauthenticated
        `POST /v1/trial` -- same reasoning as
        `hosted/ap_exceptions/service.py`'s identical middleware."""
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"request body of {declared_size} bytes exceeds the "
                            f"{MAX_REQUEST_BODY_BYTES}-byte limit"
                        )
                    },
                )
        return await call_next(request)

    @app.on_event("startup")
    async def _start_purge_loop() -> None:
        interval = float(os.environ.get("COST_GATEWAY_PURGE_INTERVAL_SECONDS", "60"))

        async def _loop() -> None:
            while True:
                await asyncio.sleep(interval)
                trial_registry.purge_expired()

        app.state.purge_task = asyncio.create_task(_loop())

    @app.on_event("shutdown")
    async def _stop_purge_loop() -> None:
        task = getattr(app.state, "purge_task", None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _authenticated_tenant(request: Request) -> Tenant:
        tenant = authenticate(request, trial_registry)
        limiter.check(tenant.tenant_id)
        return tenant

    # Computed once and reused as every route's default, rather than
    # calling Depends(_authenticated_tenant) inline at each route -- both
    # are equivalent at runtime (Depends() itself is cheap; the actual
    # dependency call still happens once per request either way), but
    # this form also satisfies ruff's B008 the way `Tenant` (a mutable
    # dataclass) can't otherwise: see ruff's own suggested fix for
    # "function-call-in-default-argument".
    _require_auth = Depends(_authenticated_tenant)

    def _stores_for(tenant: Tenant) -> TenantStores:
        return tenant_stores.get(tenant.tenant_id)

    def _require_path_tenant(tenant_id: str, tenant: Tenant) -> None:
        if tenant_id != tenant.tenant_id:
            raise HTTPException(
                status_code=403, detail="trial API key does not match tenant_id in the URL"
            )

    def _status_payload(
        tenant: Tenant, *, base_url: str, stores: TenantStores | None
    ) -> dict[str, Any]:
        key_status = key_vault.status(tenant.tenant_id)
        payload: dict[str, Any] = {
            "tenant_id": tenant.tenant_id,
            "base_url": base_url,
            "dashboard_url": None,
            "dashboard_status": "not yet available -- Phase 2 adds the hosted dashboard",
            "mode": "real_key" if tenant.has_real_key else "demo",
            "expires_at": iso(tenant.expires_at),
            "seconds_remaining": round(tenant.seconds_remaining(), 1),
            "openai_configured": key_status.openai_configured,
            "anthropic_configured": key_status.anthropic_configured,
            "rate_limit": {
                "max_requests": limiter.max_requests,
                "window_seconds": limiter.window_seconds,
            },
            "daily_budget_usd": str(daily_budget_usd),
            "notice": TRIAL_NOTICE,
        }
        if stores is not None:
            budget = stores.budgets.get(new_budget_id("global", None, "daily"))
            if budget is not None:
                spent = spent_so_far_usd(stores.receipts, budget)
                payload["spent_today_usd"] = str(spent.spent_usd)
                payload["has_unpriced_usage_today"] = spent.has_unpriced_usage
        return payload

    @app.exception_handler(InferrailError)
    async def handle_inferrail_error(_: Request, exc: InferrailError) -> JSONResponse:
        status = _status_for(exc)
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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/trial")
    async def create_trial(request: Request) -> dict[str, Any]:
        """No authentication required -- the self-serve entry point. See
        module docstring."""
        api_key, tenant = trial_registry.issue(client_ip=_client_ip(request))
        payload = _status_payload(tenant, base_url=_base_url(request), stores=None)
        payload["api_key"] = api_key
        return payload

    @app.get("/v1/trial/{tenant_id}")
    async def trial_status(
        tenant_id: str, request: Request, tenant: Tenant = _require_auth
    ) -> dict[str, Any]:
        _require_path_tenant(tenant_id, tenant)
        stores = _stores_for(tenant)
        return _status_payload(tenant, base_url=_base_url(request), stores=stores)

    @app.post("/v1/trial/{tenant_id}/keys")
    async def submit_keys(
        tenant_id: str,
        body: SubmitKeysRequest,
        request: Request,
        tenant: Tenant = _require_auth,
    ) -> dict[str, Any]:
        _require_path_tenant(tenant_id, tenant)
        if body.openai_key is None and body.anthropic_key is None:
            raise HTTPException(
                status_code=422, detail="submit at least one of openai_key/anthropic_key"
            )
        openai_key = (
            _validate_key_shape(body.openai_key, field_name="openai_key")
            if body.openai_key is not None
            else None
        )
        anthropic_key = (
            _validate_key_shape(body.anthropic_key, field_name="anthropic_key")
            if body.anthropic_key is not None
            else None
        )
        submitted_at = key_vault.set_keys(
            tenant.tenant_id, openai_key=openai_key, anthropic_key=anthropic_key
        )
        tenant.tighten_for_real_key(
            submitted_at=submitted_at, real_key_ttl_seconds=trial_registry.real_key_ttl_seconds
        )
        return _status_payload(
            tenant, base_url=_base_url(request), stores=_stores_for(tenant)
        )

    @app.delete("/v1/trial/{tenant_id}/keys")
    async def delete_keys(
        tenant_id: str, tenant: Tenant = _require_auth
    ) -> dict[str, Any]:
        """Explicit "forget my key" -- clears any stored key immediately
        without ending the trial or touching its receipts/budget data."""
        _require_path_tenant(tenant_id, tenant)
        removed = key_vault.forget(tenant.tenant_id)
        return {"tenant_id": tenant.tenant_id, "keys_removed": removed}

    @app.delete("/v1/trial/{tenant_id}")
    async def end_trial(
        tenant_id: str, tenant: Tenant = _require_auth
    ) -> dict[str, Any]:
        """Explicit, immediate teardown of the whole trial (data and any
        key) -- for a visitor who wants everything gone right now rather
        than waiting for the TTL."""
        _require_path_tenant(tenant_id, tenant)
        ended = trial_registry.end_trial(tenant.tenant_id)
        return {"tenant_id": tenant.tenant_id, "ended": ended}

    @app.get("/v1/receipts")
    async def list_receipts(
        request: Request,
        limit: int = 50,
        offset: int = 0,
        tenant: Tenant = _require_auth,
    ) -> dict[str, Any]:
        del request
        stores = _stores_for(tenant)
        bounded_limit = max(1, min(limit, 200))
        receipts = stores.receipts.query(limit=bounded_limit, offset=max(0, offset))
        total = stores.receipts.count()
        return {
            "receipts": [_receipt_json(r) for r in receipts],
            "total": total,
            "limit": bounded_limit,
            "offset": offset,
        }

    @app.post("/v1/demo/chat/completions")
    async def demo_chat_completions(
        payload: ChatCompletionRequest,
        request: Request,
        tenant: Tenant = _require_auth,
    ) -> ChatCompletionResponse:
        """Zero-key, always available regardless of a trial's real-key
        state. The `model` field is ignored: every demo request routes to
        one fixed, `DEMO`-priced canned response (see `demo_provider.py`)
        so cost is always known here, never `unknown` -- matching
        `inferrail demo`'s own honest design."""
        stores = _stores_for(tenant)
        demo_route = RouteConfig(provider=DEMO_PROVIDER_NAME, model=DEMO_MODEL_NAME)
        router = Router(routes={"demo": demo_route})
        provider = DemoProvider()
        engine = InferenceEngine(
            router,
            {DEMO_PROVIDER_NAME: provider},
            NullTelemetrySink(),
            pricing_resolver,
            stores.receipts,
            budgets=stores.enforcer,
        )
        attributes = extract_attributes(request.headers)
        forced_payload = payload.model_copy(update={"model": "demo", "stream": False})
        return await engine.execute(forced_payload, attributes=attributes)

    def _require_openai_key(tenant: Tenant) -> str:
        key = key_vault.get_openai_key(tenant.tenant_id)
        if key is None:
            raise HTTPException(
                status_code=400,
                detail="no OpenAI key configured for this trial -- submit one via "
                "POST /v1/trial/{tenant_id}/keys, or use POST /v1/demo/chat/completions "
                "for zero-key demo mode",
            )
        return key

    def _require_anthropic_key(tenant: Tenant) -> str:
        key = key_vault.get_anthropic_key(tenant.tenant_id)
        if key is None:
            raise HTTPException(
                status_code=400,
                detail="no Anthropic key configured for this trial -- submit one via "
                "POST /v1/trial/{tenant_id}/keys, or use POST /v1/demo/chat/completions "
                "for zero-key demo mode",
            )
        return key

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(
        payload: ChatCompletionRequest,
        request: Request,
        tenant: Tenant = _require_auth,
    ) -> ChatCompletionResponse | StreamingResponse:
        """Real OpenAI-compatible passthrough, using this tenant's own
        submitted key -- never Inferrail's. `model` is forwarded verbatim
        to OpenAI (docs/adr/0007 model-passthrough-routing): this
        service pre-registers no named routes, so any model id the
        tenant's own client sends just works, identical in spirit to
        `inferrail serve --quickstart`."""
        stores = _stores_for(tenant)
        api_key = _require_openai_key(tenant)
        provider = OpenAIProvider(
            name="openai",
            api_key=api_key,
            base_url="https://api.openai.com/v1",
            is_verified_openai=True,
        )
        router = Router(routes={}, default_provider="openai")
        engine = InferenceEngine(
            router,
            {"openai": provider},
            NullTelemetrySink(),
            pricing_resolver,
            stores.receipts,
            budgets=stores.enforcer,
        )
        attributes = extract_attributes(request.headers)
        if payload.stream:
            try:
                body = await engine.prepare_stream(payload, attributes=attributes)
            except BaseException:
                await provider.aclose()
                raise
            return StreamingResponse(
                _stream_and_close(body, provider), media_type="text/event-stream"
            )
        try:
            return await engine.execute(payload, attributes=attributes)
        finally:
            await provider.aclose()

    @app.post("/v1/messages", response_model=None)
    async def messages(
        payload: MessagesRequest,
        request: Request,
        tenant: Tenant = _require_auth,
    ) -> MessagesResponse | StreamingResponse:
        """Real Anthropic-compatible passthrough, using this tenant's own
        submitted key -- never Inferrail's. Same passthrough-routing
        reasoning as `chat_completions` above."""
        stores = _stores_for(tenant)
        api_key = _require_anthropic_key(tenant)
        provider = AnthropicProvider(
            name="anthropic", api_key=api_key, base_url="https://api.anthropic.com/v1"
        )
        router = Router(routes={}, default_provider="anthropic")
        engine = AnthropicInferenceEngine(
            router,
            {"anthropic": provider},
            NullTelemetrySink(),
            pricing_resolver,
            stores.receipts,
            budgets=stores.enforcer,
        )
        attributes = extract_attributes(request.headers)
        if payload.stream:
            try:
                body = await engine.prepare_stream(payload, attributes=attributes)
            except BaseException:
                await provider.aclose()
                raise
            return StreamingResponse(
                _stream_and_close(body, provider), media_type="text/event-stream"
            )
        try:
            return await engine.execute(payload, attributes=attributes)
        finally:
            await provider.aclose()

    return app


if __name__ == "__main__":
    import sys

    import uvicorn

    # A bare `python3 service.py` (no CLI args) is the production/hosted
    # shape: bind 0.0.0.0 and read the platform-injected $PORT. Explicit
    # argv[1] (data_dir) / argv[2] (port) is the local/loopback test shape
    # -- same convention as `hosted/ap_exceptions/service.py`.
    explicit_args = len(sys.argv) > 1
    data_dir = (
        Path(sys.argv[1])
        if explicit_args
        else Path(os.environ.get("COST_GATEWAY_DATA_DIR", "/tmp/inferrail_cost_gateway"))
    )
    port = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("PORT", 8423))
    host = "127.0.0.1" if explicit_args else "0.0.0.0"
    app = create_app(data_dir)
    print(f"Inferrail Cost Gateway listening on http://{host}:{port} (data_dir={data_dir})")
    uvicorn.run(
        app, host=host, port=port, log_level="warning", proxy_headers=True, forwarded_allow_ips="*"
    )
