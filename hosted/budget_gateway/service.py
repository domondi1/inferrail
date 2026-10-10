"""Inferrail Hosted: the Inferrail gateway run for you. Per-run budgets hold across every agent and
machine that calls the same workspace.

Lives outside `src/inferrail`, like the other hosted services (docs/adr/0004). The self-hosted
gateway is unchanged and unrestricted. What this service adds is operation: a durable workspace,
one central budget ledger that every client of the workspace shares, and self-service billing.

**Deployment constraint: exactly one instance per data directory.**
- Every guarantee comes from one process owning its SQLite files. Many client machines may call
  one instance.
- Multiple gateway replicas sharing state are **not supported**.
- `create_app` takes an exclusive OS lock on the data directory and refuses to start if
  another instance holds it.

**How a call works**
1. `Authorization: Bearer irw_...` identifies the workspace.
2. The provider key travels in `X-Provider-Api-Key` on every request. It is never stored,
   logged or returned, and it is scrubbed from any error text.
3. `X-Inferrail-Attribute-Work-Id` names the run, and `X-Inferrail-Budget-Usd` sets its ceiling.
   Concurrent calls in one run share it through the same atomic reservation as the self-hosted
   gateway.
4. The run is metered (`workspaces.py`): billed only if one of its calls is answered by the
   provider.
5. Past the free allowance with no credits, a call gets 402 `allowance_exhausted`. Nothing is
   sent to the provider.

**Credits, one ledger, two rails**
- `POST /v1/credits/x402`, agents, USDC:
  - the handler records a pending purchase, keyed by payer and EIP-3009 nonce, before
    settlement;
  - the after-settle hook grants credits;
  - a crash in between is resolved from the chain (`chain_check.py`) at startup and
    periodically.
- `POST /v1/credits/checkout`, people, card:
  - Stripe Checkout;
  - only a signature-verified webhook grants credits;
  - refunds reverse them.

Both rails are off until configured. Mainnet x402 also requires `BG_X402_MAINNET_APPROVED=1`.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chain_check  # noqa: E402
import stripe_checkout  # noqa: E402
from workspaces import WorkspaceLedger  # noqa: E402

from inferrail.budgets.enforcement import BudgetEnforcer  # noqa: E402
from inferrail.budgets.store import BudgetStore  # noqa: E402
from inferrail.config.models import ProviderConfig  # noqa: E402
from inferrail.errors import (  # noqa: E402
    AuthenticationError,
    BudgetDeclarationError,
    BudgetExceededError,
    InferrailError,
    InvalidRequestError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    RoutingError,
    UnsupportedFeatureError,
)
from inferrail.errors.codes import code_for, docs_url_for  # noqa: E402
from inferrail.gateway.anthropic_execution import AnthropicInferenceEngine  # noqa: E402
from inferrail.gateway.anthropic_schemas import (  # noqa: E402
    MessagesRequest,
    absent_cache_usage_fields,
)
from inferrail.gateway.attribution import extract_attributes, extract_declared_budget  # noqa: E402
from inferrail.gateway.execution import InferenceEngine  # noqa: E402
from inferrail.gateway.schemas import (  # noqa: E402
    ChatCompletionRequest,
    ErrorDetail,
    ErrorResponse,
)
from inferrail.pricing.resolver import PricingResolver  # noqa: E402
from inferrail.providers.anthropic import AnthropicProvider  # noqa: E402
from inferrail.providers.openai import OpenAIProvider  # noqa: E402
from inferrail.receipts.sqlite_store import ReceiptsStore  # noqa: E402
from inferrail.routing.router import Router  # noqa: E402
from inferrail.telemetry.sinks import NullTelemetrySink  # noqa: E402

log = logging.getLogger("inferrail.hosted")
PROVIDER_KEY_HEADER = "x-provider-api-key"
MAINNET = "eip155:8453"
TESTNET = "eip155:84532"
BILLING_RULE = (
    "A governed run is a distinct work id (X-Inferrail-Attribute-Work-Id) in a UTC month. It is "
    "billed only if at least one of its calls is answered by the provider; runs whose calls all "
    "fail or are refused by their own budget cost nothing. Calls without a work id are each "
    "their own run."
)

_STATUS_BY_ERROR: list[tuple[type[InferrailError], int]] = [
    (AuthenticationError, 401),
    (BudgetExceededError, 402),
    (BudgetDeclarationError, 400),
    (RateLimitError, 429),
    (ProviderTimeoutError, 504),
    (InvalidRequestError, 400),
    (UnsupportedFeatureError, 400),
    (RoutingError, 400),
    (ProviderError, 502),
]


def openai_client_factory() -> httpx.AsyncClient | None:
    """Test seam (httpx.MockTransport), same as hosted/cost_gateway."""
    return None


def anthropic_client_factory() -> httpx.AsyncClient | None:
    return None


def stripe_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient()


def authorization_checker(settings: Settings) -> Any:
    """`(authorizer, nonce) -> bool | None` against the chain, or None if no RPC is configured."""
    if not settings.rpc_url:
        return None
    return lambda authorizer, nonce: chain_check.authorization_used(
        settings.rpc_url or "", settings.x402_network, authorizer, nonce
    )


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    free_runs_per_month: int = 2000
    per_work_max_usd: Decimal = Decimal("100")
    creation_enabled: bool = True
    max_workspaces: int = 10_000
    creations_per_ip_per_hour: int = 5
    max_body_bytes: int = 2_000_000
    x402_network: str = TESTNET
    x402_pay_to: str | None = None
    x402_pack_price_usd: str = "1.00"
    x402_pack_runs: int = 1000
    rpc_url: str | None = None
    reconcile_seconds: int = 60
    stripe: stripe_checkout.StripeConfig | None = None
    public_base_url: str = "http://localhost:8424"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ if env is None else env)
        network = e.get("BG_X402_NETWORK", TESTNET)
        if network not in (MAINNET, TESTNET):
            raise ValueError(f"unsupported BG_X402_NETWORK {network}")
        pay_to = e.get("BG_X402_PAY_TO") or None
        if network == MAINNET and pay_to and e.get("BG_X402_MAINNET_APPROVED") != "1":
            raise RuntimeError("mainnet credit sales require BG_X402_MAINNET_APPROVED=1")
        if network == MAINNET and pay_to and not e.get("BG_BASE_RPC_URL"):
            # Without chain reconciliation, a payment that settled while the process crashed
            # could stay uncredited. Refuse rather than risk it.
            raise RuntimeError("mainnet credit sales require BG_BASE_RPC_URL for reconciliation")
        base = e.get("BG_PUBLIC_BASE_URL", "http://localhost:8424")
        stripe = None
        if e.get("STRIPE_SECRET_KEY") and e.get("STRIPE_WEBHOOK_SECRET"):
            stripe = stripe_checkout.StripeConfig(
                secret_key=e["STRIPE_SECRET_KEY"],
                webhook_secret=e["STRIPE_WEBHOOK_SECRET"],
                pack_price_cents=int(e.get("BG_STRIPE_PACK_CENTS", "1000")),
                pack_runs=int(e.get("BG_STRIPE_PACK_RUNS", "10000")),
                success_url=f"{base}/v1/credits/thanks",
                cancel_url=f"{base}/pricing",
            )
        return cls(
            data_dir=Path(e.get("BG_DATA_DIR", "/tmp/inferrail_budget_gateway")),
            free_runs_per_month=int(e.get("BG_FREE_RUNS_PER_MONTH", "2000")),
            per_work_max_usd=Decimal(e.get("BG_PER_WORK_MAX_USD", "100")),
            creation_enabled=e.get("BG_WORKSPACE_CREATION_ENABLED", "1") == "1",
            max_workspaces=int(e.get("BG_MAX_WORKSPACES", "10000")),
            creations_per_ip_per_hour=int(e.get("BG_CREATIONS_PER_IP_PER_HOUR", "5")),
            max_body_bytes=int(e.get("BG_MAX_BODY_BYTES", "2000000")),
            x402_network=network,
            x402_pay_to=pay_to,
            x402_pack_price_usd=e.get("BG_X402_PACK_PRICE_USD", "1.00"),
            x402_pack_runs=int(e.get("BG_X402_PACK_RUNS", "1000")),
            rpc_url=e.get("BG_BASE_RPC_URL") or None,
            reconcile_seconds=int(e.get("BG_RECONCILE_SECONDS", "60")),
            stripe=stripe,
            public_base_url=base,
        )


class InstanceLockError(RuntimeError):
    pass


def _acquire_instance_lock(data_dir: Path) -> int:
    fd = os.open(data_dir / "instance.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as ex:
        os.close(fd)
        raise InstanceLockError(
            f"another Inferrail Hosted instance is using {data_dir}; exactly one instance per "
            "data directory is supported"
        ) from ex
    return fd


class _Stores:
    def __init__(self, data_dir: Path, pricing: PricingResolver, per_work_max: Decimal) -> None:
        self._dir, self._pricing, self._max = data_dir, pricing, per_work_max
        self._cache: dict[str, tuple[ReceiptsStore, BudgetEnforcer]] = {}
        self._lock = threading.Lock()

    def get(self, workspace_id: str) -> tuple[ReceiptsStore, BudgetEnforcer]:
        with self._lock:
            hit = self._cache.get(workspace_id)
            if hit is None:
                receipts = ReceiptsStore(self._dir / f"{workspace_id}-receipts.sqlite3")
                budgets = BudgetStore(self._dir / f"{workspace_id}-budgets.sqlite3")
                enforcer = BudgetEnforcer(
                    budgets,
                    receipts,
                    self._pricing,
                    per_work_max_usd=self._max,
                    allow_declared_budgets=True,
                )
                hit = self._cache[workspace_id] = (receipts, enforcer)
            return hit


def _pricing() -> PricingResolver:
    return PricingResolver(
        providers={
            "openai": ProviderConfig(type="openai", api_key_env="BG_UNUSED_OPENAI"),
            "anthropic": ProviderConfig(type="anthropic", api_key_env="BG_UNUSED_ANTHROPIC"),
        },
        overrides={},
    )


def _authorization(payload: Any) -> dict[str, Any]:
    auth = (getattr(payload, "payload", None) or {}).get("authorization") or {}
    return auth if isinstance(auth, dict) else {}


def _payment_ref(payload: Any) -> str | None:
    auth = _authorization(payload)
    nonce, payer = auth.get("nonce"), auth.get("from")
    return f"{str(payer).lower()}:{str(nonce).lower()}" if nonce and payer else None


def _scrub(text: str, request: Request) -> str:
    key = request.headers.get(PROVIDER_KEY_HEADER, "").strip()
    return text.replace(key, "[redacted]") if len(key) >= 8 else text


def create_app(settings: Settings, *, facilitator: Any | None = None) -> FastAPI:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = _acquire_instance_lock(settings.data_dir)
    ledger = WorkspaceLedger(
        settings.data_dir / "workspaces.sqlite3", free_runs_per_month=settings.free_runs_per_month
    )
    released = ledger.release_orphaned_holds()
    if released:
        log.warning("released %d run holds left by a previous process", released)
    pricing_resolver = _pricing()  # stateless; shared by every workspace
    stores = _Stores(settings.data_dir, pricing_resolver, settings.per_work_max_usd)
    creations: dict[str, list[float]] = {}
    checker = authorization_checker(settings) if settings.x402_pay_to else None

    def reconcile() -> dict[str, int] | None:
        return ledger.reconcile_pending(checker) if checker else None

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = None
        if checker:
            await asyncio.to_thread(reconcile)

            async def loop() -> None:
                while True:
                    await asyncio.sleep(settings.reconcile_seconds)
                    try:
                        await asyncio.to_thread(reconcile)
                    except Exception:  # keep reconciling; never take the API down
                        log.exception("reconciliation pass failed")

            task = asyncio.create_task(loop())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Inferrail Hosted", lifespan=lifespan)
    app.state.ledger = ledger
    app.state.reconcile = reconcile

    def release_instance_lock() -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    app.state.release_instance_lock = release_instance_lock

    @app.middleware("http")
    async def body_limit(request: Request, call_next: Any) -> Any:
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.max_body_bytes:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        return await call_next(request)

    # -- published terms ---------------------------------------------------------------------------
    def price_sheet() -> dict[str, Any]:
        rails: dict[str, Any] = {}
        if settings.x402_pay_to:
            rails["x402"] = {
                "endpoint": "POST /v1/credits/x402",
                "network": settings.x402_network,
                "asset": "USDC",
                "price_usd": settings.x402_pack_price_usd,
                "governed_runs": settings.x402_pack_runs,
                "account_needed": False,
            }
        if settings.stripe:
            rails["card"] = {
                "endpoint": "POST /v1/credits/checkout",
                "price_usd": f"{settings.stripe.pack_price_cents / 100:.2f}",
                "governed_runs": settings.stripe.pack_runs,
            }
        return {
            "product": "Inferrail Hosted",
            "status": "experimental pricing",
            "billable_unit": BILLING_RULE,
            "free_governed_runs_per_month": settings.free_runs_per_month,
            "price_per_governed_run_usd": "0.001",
            "credits": rails,
            "no_surprise_charges": (
                "Credits are prepaid; there is no subscription, no auto top-up and no overage "
                "billing. When the allowance and credits run out, calls are refused with 402 "
                "before reaching the provider."
            ),
            "subscription": None,
            "self_hosted": (
                "The open-source gateway is free and unrestricted: pip install inferrail"
            ),
            "provider_spend": (
                "Billed by your provider on your own key. Inferrail never resells model access "
                "and never stores your provider key."
            ),
            "deployment": "single instance; budgets are shared by every client of a workspace",
        }

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/pricing")
    async def pricing() -> dict[str, Any]:
        return price_sheet()

    @app.get("/llms.txt", response_class=PlainTextResponse)
    async def llms() -> str:
        p = price_sheet()
        return (
            "# Inferrail Hosted\n\n> Per-run dollar budgets for AI agents, reserved before every "
            "provider call and shared by every agent and machine using one workspace. "
            "Payload-free receipts. Bring your own OpenAI or Anthropic key.\n\n"
            f"- Create a workspace: POST {settings.public_base_url}/v1/workspaces (no account)\n"
            "- OpenAI-compatible: POST /v1/chat/completions; Anthropic-compatible: POST "
            "/v1/messages\n"
            "- Headers: Authorization: Bearer <workspace key>; X-Provider-Api-Key: <your provider "
            "key>; X-Inferrail-Attribute-Work-Id: <run id>; X-Inferrail-Budget-Usd: <ceiling>\n"
            f"- Billing: {BILLING_RULE}\n"
            f"- Price: {p['free_governed_runs_per_month']} governed runs/month free, then "
            f"${p['price_per_governed_run_usd']} per run via prepaid credits: GET /pricing\n"
        )

    # -- workspaces -------------------------------------------------------------------------------
    @app.post("/v1/workspaces", status_code=201)
    async def create_workspace(request: Request) -> dict[str, Any]:
        if not settings.creation_enabled:
            raise HTTPException(503, "workspace creation is paused")
        if ledger.count_workspaces() >= settings.max_workspaces:
            raise HTTPException(503, "workspace capacity reached")
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        recent = [t for t in creations.get(ip, []) if now - t < 3600]
        if len(recent) >= settings.creations_per_ip_per_hour:
            raise HTTPException(429, "too many workspaces created from this address")
        creations[ip] = [*recent, now]
        workspace_id, api_key = ledger.create_workspace()
        return {
            "workspace_id": workspace_id,
            "api_key": api_key,
            "api_key_notice": "Shown once. Inferrail stores only its hash.",
            "base_url": settings.public_base_url + "/v1",
            "next": "Send a call with Authorization: Bearer <api_key>, X-Provider-Api-Key, "
            "X-Inferrail-Attribute-Work-Id and X-Inferrail-Budget-Usd.",
            "pricing": price_sheet(),
        }

    def _workspace(request: Request) -> str:
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        workspace_id = ledger.authenticate(token) if token else None
        if workspace_id is None:
            raise HTTPException(401, "missing or invalid workspace key")
        return workspace_id

    require_workspace = Depends(_workspace)

    @app.get("/v1/workspace")
    async def workspace_status(workspace_id: str = require_workspace) -> dict[str, Any]:
        u = ledger.usage(workspace_id)
        return {
            "workspace_id": workspace_id,
            "month": u.month,
            "governed_runs": u.month_runs,
            "billed_runs": u.billed_runs,
            "free_runs_remaining": u.free_runs_remaining,
            "credits_remaining": u.credits_remaining,
            "pricing": price_sheet(),
        }

    @app.get("/v1/workspace/ledger")
    async def workspace_ledger(workspace_id: str = require_workspace) -> dict[str, Any]:
        return ledger.statement(workspace_id)

    # -- proxy ------------------------------------------------------------------------------------
    @app.exception_handler(InferrailError)
    async def inferrail_error(request: Request, exc: InferrailError) -> JSONResponse:
        status = next((s for t, s in _STATUS_BY_ERROR if isinstance(exc, t)), 500)
        code = code_for(exc)
        body = ErrorResponse(
            error=ErrorDetail(
                message=_scrub(str(exc), request),
                type=type(exc).__name__,
                code=code.code,
                remediation=code.remediation,
                docs_url=docs_url_for(code.code),
            )
        )
        return JSONResponse(status_code=status, content=body.model_dump())

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled %s", type(exc).__name__)  # never the message: it could hold a key
        return JSONResponse({"detail": "internal error"}, status_code=500)

    def _admit(request: Request, workspace_id: str) -> tuple[dict[str, str], Decimal | None, Any]:
        provider_key = request.headers.get(PROVIDER_KEY_HEADER, "").strip()
        if not provider_key:
            raise HTTPException(400, "send your provider key in X-Provider-Api-Key")
        attributes = extract_attributes(request.headers)
        declared = extract_declared_budget(request.headers)
        work_id = attributes.setdefault("work_id", f"call_{uuid.uuid4().hex[:16]}")
        admission = ledger.hold_run(workspace_id, work_id)
        if not admission.admitted:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "allowance_exhausted",
                    "message": "This month's free governed runs are used and no credits remain. "
                    "Buy credits to continue; nothing was sent to the provider.",
                    "purchase": price_sheet()["credits"],
                },
            )
        return attributes, declared, (workspace_id, admission.month, work_id)

    async def _run(
        engine: Any,
        provider: Any,
        payload: Any,
        request: Request,
        workspace_id: str,
        serialize: Any,
    ) -> Any:
        attributes, declared, hold = _admit(request, workspace_id)
        if payload.stream:
            try:
                body = await engine.prepare_stream(
                    payload, attributes=attributes, declared_budget_usd=declared
                )
            except BaseException:
                ledger.finish_call(*hold, answered=False)
                await provider.aclose()
                raise

            async def stream() -> AsyncIterator[bytes]:
                try:
                    async for chunk in body:
                        yield chunk
                finally:
                    ledger.finish_call(*hold, answered=True)
                    await provider.aclose()

            return StreamingResponse(stream(), media_type="text/event-stream")
        answered = False
        try:
            result = await engine.execute(
                payload, attributes=attributes, declared_budget_usd=declared
            )
            answered = True
            return serialize(result)
        finally:
            ledger.finish_call(*hold, answered=answered)
            await provider.aclose()

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(
        payload: ChatCompletionRequest, request: Request, workspace_id: str = require_workspace
    ) -> Any:
        receipts, enforcer = stores.get(workspace_id)
        provider = OpenAIProvider(
            name="openai",
            api_key=request.headers.get(PROVIDER_KEY_HEADER, "").strip(),
            base_url="https://api.openai.com/v1",
            is_verified_openai=True,
            client=openai_client_factory(),
        )
        engine = InferenceEngine(
            Router(routes={}, default_provider="openai"),
            {"openai": provider},
            NullTelemetrySink(),
            pricing_resolver,
            receipts,
            budgets=enforcer,
        )
        return await _run(engine, provider, payload, request, workspace_id, lambda r: r)

    @app.post("/v1/messages", response_model=None)
    async def messages(
        payload: MessagesRequest, request: Request, workspace_id: str = require_workspace
    ) -> Any:
        receipts, enforcer = stores.get(workspace_id)
        provider = AnthropicProvider(
            name="anthropic",
            api_key=request.headers.get(PROVIDER_KEY_HEADER, "").strip(),
            base_url="https://api.anthropic.com/v1",
            client=anthropic_client_factory(),
        )
        engine = AnthropicInferenceEngine(
            Router(routes={}, default_provider="anthropic"),
            {"anthropic": provider},
            NullTelemetrySink(),
            pricing_resolver,
            receipts,
            budgets=enforcer,
        )
        return await _run(
            engine,
            provider,
            payload,
            request,
            workspace_id,
            lambda r: JSONResponse(
                r.model_dump(mode="json", exclude={"usage": absent_cache_usage_fields(r.usage)})
            ),
        )

    @app.get("/v1/work/{work_id}")
    async def work(work_id: str, workspace_id: str = require_workspace) -> dict[str, Any]:
        receipts, _ = stores.get(workspace_id)
        rows = receipts.query(work_id=work_id)
        priced = [r.estimated_cost_usd for r in rows if r.estimated_cost_usd is not None]
        return {
            "work_id": work_id,
            "calls": len(rows),
            "cost_usd": str(sum(priced, Decimal(0))),
            "unpriced_calls": len(rows) - len(priced),
            "unsuccessful_calls": sum(1 for r in rows if r.status != "success"),
            "receipts": [
                {
                    "request_id": r.request_id,
                    "model": r.model,
                    "status": r.status,
                    "estimated_cost_usd": None
                    if r.estimated_cost_usd is None
                    else str(r.estimated_cost_usd),
                }
                for r in rows
            ],
        }

    # -- credits: card ----------------------------------------------------------------------------
    @app.post("/v1/credits/checkout")
    async def checkout(request: Request, workspace_id: str = require_workspace) -> dict[str, str]:
        if settings.stripe is None:
            raise HTTPException(501, "card purchases are not enabled on this deployment")
        packs_raw = request.query_params.get("packs", "1")
        if not packs_raw.isdigit() or not 1 <= int(packs_raw) <= 100:
            raise HTTPException(400, "packs must be 1-100")
        async with stripe_client_factory() as client:
            url = await stripe_checkout.create_session(
                settings.stripe, workspace_id, int(packs_raw), client
            )
        return {"checkout_url": url, "note": "Credits are added when Stripe confirms payment."}

    @app.post("/v1/stripe/webhook")
    async def stripe_webhook(request: Request) -> dict[str, Any]:
        if settings.stripe is None:
            raise HTTPException(404)
        raw = await request.body()
        try:
            event = stripe_checkout.verify_event(
                raw, request.headers.get("stripe-signature", ""), settings.stripe.webhook_secret
            )
        except stripe_checkout.SignatureError as ex:
            raise HTTPException(400, f"invalid signature: {ex}") from ex
        refund = stripe_checkout.refunded_charge(event)
        if refund is not None:
            intent, refunded = refund
            reversed_runs = ledger.apply_refund("stripe", None, refunded, external_id=intent)
            return {"refund_applied": reversed_runs is not None, "runs_reversed": reversed_runs}
        paid = stripe_checkout.paid_session(event)
        if paid is None:
            return {"granted": False, "reason": "not a paid checkout session"}
        session_id, workspace_id, packs, amount, intent = paid
        if amount != packs * settings.stripe.pack_price_cents:
            log.error(
                "checkout %s amount %d does not match the published price", session_id, amount
            )
            return {"granted": False, "reason": "amount does not match the published price"}
        granted = ledger.grant(
            "stripe",
            session_id,
            workspace_id,
            packs * settings.stripe.pack_runs,
            amount,
            external_id=intent,
        )
        return {"granted": granted}

    @app.get("/v1/credits/thanks", response_class=PlainTextResponse)
    async def thanks() -> str:
        return "Payment submitted. Credits appear once Stripe confirms it (usually seconds)."

    # -- credits: x402 ----------------------------------------------------------------------------
    if settings.x402_pay_to:
        from x402.http import HTTPFacilitatorClient
        from x402.http.middleware.fastapi import payment_middleware
        from x402.http.types import PaymentOption, RouteConfig
        from x402.http.utils import decode_payment_signature_header
        from x402.mechanisms.evm.exact import register_exact_evm_server
        from x402.server import x402ResourceServer

        if facilitator is None:
            if settings.x402_network == MAINNET:
                from cdp.x402 import create_facilitator_config

                facilitator = HTTPFacilitatorClient(
                    create_facilitator_config(
                        api_key_id=os.environ["CDP_API_KEY_ID"],
                        api_key_secret=os.environ["CDP_API_KEY_SECRET"],
                    )
                )
            else:
                facilitator = HTTPFacilitatorClient()
        server = x402ResourceServer(facilitator)
        register_exact_evm_server(server, networks=settings.x402_network)
        pack_cents = int(Decimal(settings.x402_pack_price_usd) * 100)

        async def after_settle(context: Any) -> None:
            ref = _payment_ref(context.payment_payload)
            if ref is None:
                return
            if getattr(context.result, "success", False):
                ledger.settle_pending("x402", ref)
            else:
                ledger.fail_pending("x402", ref)

        async def settle_failed(context: Any) -> None:
            # A failed settle call is not proof the transfer didn't happen (timeouts).
            # Reconciliation decides from the chain; nothing is granted here.
            log.warning("x402 settlement failed; left pending for reconciliation")

        server.on_after_settle(after_settle)
        server.on_settle_failure(settle_failed)
        routes = {
            "POST /v1/credits/x402": RouteConfig(
                accepts=PaymentOption(
                    scheme="exact",
                    pay_to=settings.x402_pay_to,
                    price=f"${settings.x402_pack_price_usd}",
                    network=settings.x402_network,
                ),
                resource=settings.public_base_url + "/v1/credits/x402",
                description=(
                    f"{settings.x402_pack_runs:,} governed runs of Inferrail Hosted: per-run "
                    "dollar budgets for AI agents, reserved before every provider call. Send "
                    "your workspace key as a Bearer token."
                ),
                mime_type="application/json",
                service_name="Inferrail Hosted credits",
                tags=["agent-budgets", "llm-gateway", "spend-control"],
            )
        }
        x402_gate = payment_middleware(routes, server)

        @app.middleware("http")
        async def _x402(request: Request, call_next: Any) -> Any:
            return await x402_gate(request, call_next)

        @app.post("/v1/credits/x402")
        async def buy_credits_x402(
            request: Request, workspace_id: str = require_workspace
        ) -> dict[str, Any]:
            header = request.headers.get("payment-signature")
            payload = decode_payment_signature_header(header) if header else None
            ref = _payment_ref(payload) if payload else None
            auth = _authorization(payload)
            if ref is None or not auth.get("validBefore"):
                raise HTTPException(400, "missing x402 payment")
            ledger.record_pending(
                "x402",
                ref,
                workspace_id,
                settings.x402_pack_runs,
                pack_cents,
                authorizer=str(auth["from"]),
                nonce=str(auth["nonce"]),
                valid_before=int(auth["validBefore"]),
            )
            return {
                "workspace_id": workspace_id,
                "governed_runs": settings.x402_pack_runs,
                "status": "credits are added once the payment settles",
            }

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        create_app(Settings.from_env()),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8424")),
        proxy_headers=True,
        # Client IPs (creation throttle) come from X-Forwarded-For only from these proxies.
        forwarded_allow_ips=os.environ.get("BG_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        workers=1,  # one process: see the deployment constraint above
    )
