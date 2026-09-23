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
import json
import os
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
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
from inferrail.receipts.aggregation import summarize_receipts
from inferrail.receipts.schema import InferenceReceipt
from inferrail.routing.router import Router
from inferrail.telemetry.sinks import NullTelemetrySink
from inferrail.transactions.builder import build_transaction
from inferrail.work.builder import (
    aggregate_work_summaries,
    append_outcome,
    build_work_summary,
    load_outcomes,
)
from inferrail.work.schema import WorkOutcomeRecord, WorkSummary

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


def openai_client_factory() -> httpx.AsyncClient | None:
    """Returns the `httpx.AsyncClient` to inject into a per-request
    `OpenAIProvider`, or `None` for its own real default. Exists purely
    as a monkeypatch seam for tests -- `OpenAIProvider.__init__` already
    accepts an injectable client specifically "so tests can pass an
    httpx.MockTransport instead of hitting the network, while exercising
    the exact same request-building and error-normalization code paths"
    (its own docstring); this module-level function is what lets a test
    reach that seam without service.py exposing a public test-only
    parameter on `create_app`. See
    tests/unit/hosted/test_cost_gateway_service.py's real-provider-parity
    tests."""
    return None


def anthropic_client_factory() -> httpx.AsyncClient | None:
    """Same seam as `openai_client_factory`, for `AnthropicProvider`."""
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


class OutcomeRequest(BaseModel):
    """Module-level, not nested inside `create_app` -- with `from
    __future__ import annotations` active in this file, a route handler's
    string annotation for a body parameter can only be resolved against
    the module's globals; a function-locally-defined Pydantic model isn't
    reachable there, and FastAPI silently falls back to treating the
    parameter as an (always-missing) query param instead of a request
    body. Caught by `test_work_outcome_and_summary_roundtrip`."""

    model_config = {"extra": "forbid"}

    outcome_status: str


class FeedbackRequest(BaseModel):
    """Module-level for the same reason `OutcomeRequest` is -- see its
    docstring."""

    model_config = {"extra": "forbid"}

    message: str
    contact: str | None = None
    """Optional -- an email or any other way the founder could reply.
    Never required: a visitor can report a problem without identifying
    themselves."""


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


def _work_summary_json(summary: WorkSummary) -> dict[str, Any]:
    return {
        "work_id": summary.work_id,
        "outcome_status": summary.outcome_status,
        "outcome_recorded_at": (
            summary.outcome_recorded_at.isoformat() if summary.outcome_recorded_at else None
        ),
        "started_at": summary.started_at.isoformat() if summary.started_at else None,
        "ended_at": summary.ended_at.isoformat() if summary.ended_at else None,
        "receipt_count": summary.receipt_count,
        "known_attributed_inference_cost_usd": (
            str(summary.known_attributed_inference_cost_usd)
            if summary.known_attributed_inference_cost_usd is not None
            else None
        ),
        "unknown_cost_count": summary.unknown_cost_count,
        "inference_status": summary.inference_status,
    }


async def _create_github_issue(record: dict[str, Any]) -> str | None:
    """Best-effort: files `record` as a GitHub Issue on
    `COST_GATEWAY_GITHUB_REPO` (default `domondi1/inferrail`), so
    feedback survives a Render redeploy even though the local
    `feedback.jsonl` file (on Render's free tier, with no persistent
    disk) does not. Returns the created issue's URL, or `None` if
    `COST_GATEWAY_GITHUB_TOKEN` isn't configured or the API call fails
    for any reason.

    Deliberately never raises and never blocks a feedback submission
    from succeeding: this is an enhancement over the always-on local
    write in `_append_feedback`, not a replacement for it, and a
    visitor's feedback must still be recorded even if GitHub is
    unreachable or the token is misconfigured.

    The token needs only "Issues: write" on this one repository -- a
    fine-grained GitHub personal access token scoped that narrowly,
    never a classic PAT with broader repo access, minimizes what a
    leaked token could do.
    """
    token = os.environ.get("COST_GATEWAY_GITHUB_TOKEN")
    if not token:
        return None
    repo = os.environ.get("COST_GATEWAY_GITHUB_REPO", "domondi1/inferrail")
    title_source = record["message"].splitlines()[0][:80]
    title = f"[Cost Gateway feedback] {title_source}"
    body_lines = [
        record["message"],
        "",
        "---",
        f"Submitted: {record['submitted_at']}",
        f"Tenant: `{record['tenant_id']}`",
        f"Contact: {record['contact'] or '(none given)'}",
    ]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                json={
                    "title": title,
                    "body": "\n".join(body_lines),
                    "labels": ["cost-gateway-feedback"],
                },
            )
        if resp.status_code == 201:
            return str(resp.json().get("html_url"))
    except httpx.HTTPError:
        pass
    return None


def _append_feedback(path: Path, record: dict[str, Any]) -> None:
    """Appends one feedback record as a JSONL line -- a global file (not
    per-tenant), so a report about a bug survives the trial that filed
    it expiring or being ended; same append-only-file discipline as
    `inferrail.work.builder.append_outcome`, reimplemented inline here
    since the shape (free-text feedback, not a WorkOutcomeRecord) is
    different enough not to share that function directly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _read_feedback(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


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
    feedback_path = data_dir / "feedback.jsonl"
    # A global file, not per-tenant: unset by default. Admin routes are
    # entirely disabled (404, not just unauthenticated) unless an
    # operator explicitly sets this -- fail closed, never expose
    # visitor feedback or usage counts with no auth configured.
    admin_token = os.environ.get("COST_GATEWAY_ADMIN_TOKEN") or None

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

    def _authenticated_admin(request: Request) -> None:
        """A second, separate credential from trial tokens -- admin
        routes read across every tenant (feedback, usage counts), which
        no trial token should ever be able to do. `404`, not `401`, when
        no admin token is configured at all: this tells a would-be
        prober nothing about whether admin functionality exists on this
        deployment."""
        if admin_token is None:
            raise HTTPException(status_code=404)
        header = request.headers.get("authorization", "")
        provided = header[len("Bearer ") :].strip() if header.lower().startswith("bearer ") else ""
        if not provided or not secrets.compare_digest(provided, admin_token):
            raise HTTPException(status_code=401, detail="invalid or missing admin token")

    _require_admin = Depends(_authenticated_admin)

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

    @app.post("/v1/work/{work_id}/outcome")
    async def record_work_outcome(
        work_id: str,
        body: OutcomeRequest,
        tenant: Tenant = _require_auth,
    ) -> dict[str, Any]:
        """Declares a customer-defined outcome for `work_id` -- the hosted
        equivalent of `inferrail work outcome`. Reuses
        `inferrail.work.builder.append_outcome`/`WorkOutcomeRecord`
        directly (same append-only JSONL sink shape the local CLI
        writes), so the aggregation semantics (last-appended-wins,
        evidence never overwritten) are identical, not reimplemented."""
        stores = _stores_for(tenant)
        if not body.outcome_status.strip():
            raise HTTPException(status_code=422, detail="outcome_status must not be empty")
        record = WorkOutcomeRecord(
            work_id=work_id, outcome_status=body.outcome_status, recorded_at=datetime.now(UTC)
        )
        append_outcome(stores.outcomes_path, record)
        return {"work_id": work_id, "outcome_status": record.outcome_status}

    @app.get("/v1/work/{work_id}")
    async def get_work_summary(
        work_id: str, tenant: Tenant = _require_auth
    ) -> dict[str, Any]:
        """Hosted equivalent of `inferrail work <work_id>`: receipts
        sharing this `work_id` (via the `X-Inferrail-Attribute-Work-Id`
        header, exactly as self-hosted) joined with the latest declared
        outcome for it. Reuses `inferrail.work.builder.build_work_summary`
        unmodified."""
        stores = _stores_for(tenant)
        receipts = stores.receipts.query(work_id=work_id)
        outcomes, _skipped = load_outcomes(stores.outcomes_path)
        matching_outcomes = [o for o in outcomes if o.work_id == work_id]
        summary = build_work_summary(work_id, receipts, matching_outcomes)
        if summary is None:
            raise HTTPException(status_code=404, detail=f"no work found for work_id={work_id!r}")
        return _work_summary_json(summary)

    @app.get("/v1/work")
    async def list_work_summaries(tenant: Tenant = _require_auth) -> dict[str, Any]:
        """Hosted equivalent of `inferrail work --all`."""
        stores = _stores_for(tenant)
        receipts = stores.receipts.query()
        outcomes, _skipped = load_outcomes(stores.outcomes_path)
        summaries = aggregate_work_summaries(receipts, outcomes)
        return {"work": [_work_summary_json(s) for s in summaries]}

    @app.get("/v1/transaction/{task_id}")
    async def get_transaction(
        task_id: str, attribute_name: str = "task_id", tenant: Tenant = _require_auth
    ) -> dict[str, Any]:
        """Hosted equivalent of `inferrail transaction <task_id>`: every
        receipt sharing one attribution-attribute value (default
        `task_id`, same as the CLI), aggregated into one
        `TaskTransaction`. Reuses
        `inferrail.transactions.builder.build_transaction` unmodified."""
        stores = _stores_for(tenant)
        receipts = stores.receipts.query()
        transaction = build_transaction(task_id, receipts, attribute_name=attribute_name)
        if transaction is None:
            raise HTTPException(
                status_code=404,
                detail=f"no receipts found with {attribute_name}={task_id!r}",
            )
        return {
            "transaction_id": transaction.transaction_id,
            "task_id": transaction.task_id,
            "events": [
                {
                    "event_type": e.event_type,
                    "event_id": e.event_id,
                    "cost_usd": str(e.cost_usd) if e.cost_usd is not None else None,
                    "status": e.status,
                }
                for e in transaction.events
            ],
            "known_total_cost_usd": str(transaction.known_total_cost_usd),
            "unknown_cost_event_count": transaction.unknown_cost_event_count,
            "status": transaction.status,
            "started_at": transaction.started_at.isoformat(),
            "ended_at": transaction.ended_at.isoformat(),
        }

    @app.get("/v1/report")
    async def report(by: str, tenant: Tenant = _require_auth) -> dict[str, Any]:
        """Hosted equivalent of `inferrail report --by <attribute>`:
        groups every receipt by `attributes.get(by)` (receipts missing
        that attribute are grouped under `"(unattributed)"`, the same
        label the CLI uses) and summarizes each group's known cost via
        `inferrail.receipts.aggregation.summarize_receipts` -- the same
        primitive `inferrail work`/`inferrail transaction` build on, so
        every rollup surface in this service agrees on one definition of
        "known cost"."""
        stores = _stores_for(tenant)
        receipts = stores.receipts.query()
        groups: dict[str, list[InferenceReceipt]] = {}
        for r in receipts:
            groups.setdefault(r.attributes.get(by, "(unattributed)"), []).append(r)
        rows = []
        for key in sorted(groups):
            economics = summarize_receipts(groups[key])
            rows.append(
                {
                    "group": key,
                    "receipt_count": len(groups[key]),
                    "known_cost_usd": str(economics.known_cost_usd),
                    "unknown_cost_count": economics.unknown_cost_count,
                    "status": economics.status,
                }
            )
        return {"by": by, "rows": rows}

    @app.post("/v1/feedback")
    async def submit_feedback(
        body: FeedbackRequest, tenant: Tenant = _require_auth
    ) -> dict[str, Any]:
        """Free-text feedback/bug report, tied to the reporting tenant
        for context but stored globally (`feedback.jsonl` in
        `COST_GATEWAY_DATA_DIR`) so it survives that tenant's trial
        ending or expiring. Never includes a provider key -- there is no
        field for one, and this handler never touches `key_vault`."""
        message = body.message.strip()
        if not message:
            raise HTTPException(status_code=422, detail="message must not be empty")
        if len(message) > 4000:
            raise HTTPException(status_code=422, detail="message exceeds the 4000-character limit")
        contact = body.contact.strip() if body.contact else None
        record = {
            "feedback_id": f"fb_{secrets.token_urlsafe(12)}",
            "tenant_id": tenant.tenant_id,
            "message": message,
            "contact": contact,
            "submitted_at": datetime.now(UTC).isoformat(),
        }
        _append_feedback(feedback_path, record)
        # Local write above always happens and always succeeds first --
        # GitHub is a best-effort second destination for durability
        # across redeploys, never a dependency for this route returning
        # success. See _create_github_issue's own docstring.
        github_issue_url = await _create_github_issue(record)
        return {
            "feedback_id": record["feedback_id"],
            "received": True,
            "github_issue_url": github_issue_url,
        }

    @app.get("/v1/admin/feedback")
    async def list_feedback(_admin: None = _require_admin) -> dict[str, Any]:
        """Operator-only: every piece of feedback ever submitted, most
        recent first. Requires `COST_GATEWAY_ADMIN_TOKEN` -- see
        `_authenticated_admin`."""
        rows = _read_feedback(feedback_path)
        return {"feedback": list(reversed(rows)), "total": len(rows)}

    @app.get("/v1/admin/stats")
    async def admin_stats(_admin: None = _require_admin) -> dict[str, Any]:
        """Operator-only, basic usage counters -- deliberately not public:
        even aggregate numbers are worth keeping off an unauthenticated
        route. Counts *trials issued*, not unique people (no account
        system exists yet to tell those apart) -- see
        `trial.TrialRegistry.total_issued_count`'s own docstring."""
        return {
            "trials_issued_total": trial_registry.total_issued_count(),
            "trials_live_now": trial_registry.live_tenant_count(),
            "feedback_count": len(_read_feedback(feedback_path)),
            "process_local_note": (
                "These counters reset on every process restart/redeploy -- "
                "they are not a durable historical record."
            ),
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
            client=openai_client_factory(),
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
            name="anthropic",
            api_key=api_key,
            base_url="https://api.anthropic.com/v1",
            client=anthropic_client_factory(),
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
