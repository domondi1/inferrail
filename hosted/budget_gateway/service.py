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
from collections import defaultdict, deque
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

# The engine's remediation text is written for the self-hosted gateway (inferrail.yaml, CLI
# commands). A Hosted customer can't act on those, so these codes get Hosted wording. The codes
# themselves are unchanged.
_HOSTED_REMEDIATION = {
    "INFERRAIL_E002": "Your provider rejected the key sent in X-Provider-Api-Key. Check that "
    "header; Inferrail never stores provider keys.",
    "INFERRAIL_E003": "Your provider is rate-limiting your key. Safe to retry with backoff.",
    "INFERRAIL_E004": "The provider call timed out. Safe to retry.",
    "INFERRAIL_E007": "Send a model id your provider accepts (e.g. gpt-4o-mini, claude-haiku-4-5).",
    "INFERRAIL_E008": "This is a service-side problem. Retry later.",
    "INFERRAIL_E010": "This run's ceiling (X-Inferrail-Budget-Usd) would be exceeded. A run "
    "keeps its first ceiling. Not retryable as-is: start a new work id with a larger ceiling "
    "only if the extra spend is intended.",
    "INFERRAIL_E012": "A budgeted run needs a model with a known price. Use a priced model, or "
    "send the call without X-Inferrail-Budget-Usd.",
    "INFERRAIL_E013": "Send X-Inferrail-Budget-Usd as a positive decimal (at most the per-run "
    "maximum in GET /pricing) together with X-Inferrail-Attribute-Work-Id, and the same value "
    "on every call of the run.",
}

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
    free_runs_per_month: int = 250
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
    mount_prefix: str = ""
    client_ip_header: str = "true-client-ip"
    max_daily_sales_usd: Decimal = Decimal("100")
    max_outstanding_credits_usd: Decimal = Decimal("100")
    rate_limit_per_minute: int = 600

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
            free_runs_per_month=int(e.get("BG_FREE_RUNS_PER_MONTH", "250")),
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
            mount_prefix=e.get("BG_MOUNT_PREFIX", "").rstrip("/"),
            client_ip_header=e.get("BG_CLIENT_IP_HEADER", "true-client-ip").strip().lower(),
            max_daily_sales_usd=Decimal(e.get("BG_MAX_DAILY_SALES_USD", "100")),
            max_outstanding_credits_usd=Decimal(e.get("BG_MAX_OUTSTANDING_CREDITS_USD", "100")),
            rate_limit_per_minute=int(e.get("BG_RATE_LIMIT_PER_MINUTE", "600")),
        )


class _BodyLimit:
    """Refuse oversized request bodies, with or without `Content-Length` (chunked uploads are
    counted as they arrive). Bodies under the limit are buffered and replayed unchanged."""

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope.get("headers") or []).get(b"content-length", b"")
        if length.isdigit() and int(length) > self.max_bytes:
            await JSONResponse({"detail": "request body too large"}, 413)(scope, receive, send)
            return
        chunks: list[bytes] = []
        total, more = 0, True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > self.max_bytes:
                await JSONResponse({"detail": "request body too large"}, 413)(scope, receive, send)
                return
            chunks.append(body)
            more = message.get("more_body", False)
        data, replayed = b"".join(chunks), False

        async def replay() -> Any:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": data, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


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


def _agent_guide(p: dict[str, Any], settings: Settings) -> str:
    """`/llms.txt`: the whole contract in one plain-text page, generated from live settings so
    it can't drift from what this deployment actually does."""
    base = settings.public_base_url
    x402 = p["credits"].get("x402")
    card = p["credits"].get("card")
    if x402:
        buy = (
            f"- USDC over x402 ({x402['network']}): POST {x402['url']} with your workspace key. "
            "The first response is HTTP 402 with a PAYMENT-REQUIRED header (x402 v2, scheme "
            f"exact, ${x402['price_usd']} to {x402['pay_to']}). Sign it with your wallet and "
            "repeat the same request with the PAYMENT-SIGNATURE header. "
            f"One pack = {x402['governed_runs']:,} governed runs. No account.\n"
            "- Each signed payment (payer + nonce) adds credits at most once; re-sending the "
            "same signed payment can't charge twice. If a paid request times out, do NOT sign a "
            "new payment: check GET /v1/workspace/ledger. A pending purchase is settled or "
            "failed from on-chain state within minutes.\n"
            "- 503 on purchase: a sales limit is reached; nothing is charged. Retry later.\n"
        )
    else:
        buy = "- Credit purchases are not open on this deployment yet.\n"
    if card:
        buy += f"- Card: POST {card['endpoint']} returns a Stripe Checkout URL.\n"
    return (
        "# Inferrail Hosted\n\n"
        "> Per-run dollar budgets for AI agents. A call that would push a run past its ceiling "
        "is refused before it reaches OpenAI or Anthropic. The ceiling is shared by every "
        "agent and machine using the same workspace and work id. Bring your own provider key; "
        "it is never stored. Receipts hold costs and ids, never prompts or responses.\n\n"
        "## Start (no account)\n"
        f'1. POST {base}/v1/workspaces -> {{"api_key": "irw_..."}}. Shown once; keep it.\n'
        "2. Send every call with:\n"
        "   - workspace key: `Authorization: Bearer <api_key>` or `x-api-key: <api_key>`\n"
        "   - provider key: `X-Provider-Api-Key: <your OpenAI or Anthropic key>`\n"
        "   - run id: `X-Inferrail-Attribute-Work-Id: <id>` (one id per task/run)\n"
        "   - ceiling: `X-Inferrail-Budget-Usd: <usd>` (e.g. 0.50; max "
        f"{p['per_run_budget_max_usd']})\n"
        f"3. OpenAI shape: POST {base}/v1/chat/completions (OpenAI SDK base_url = {base}/v1, "
        "api_key = workspace key).\n"
        f"   Anthropic shape: POST {base}/v1/messages (Anthropic SDK base_url = {base}, "
        "api_key = workspace key).\n\n"
        "## Budgets\n"
        "- The first ceiling declared for a work id is kept for the whole run; a different "
        "value later is refused (400).\n"
        "- Without X-Inferrail-Budget-Usd a call is metered and receipted but not capped.\n"
        "- Over the ceiling: HTTP 402, error.type BudgetExceededError, code INFERRAIL_E010. "
        "Nothing reached the provider. Not retryable as-is.\n\n"
        "## Usage and receipts\n"
        f"- GET {base}/v1/workspace: runs this month, free runs left, credits left.\n"
        f"- GET {base}/v1/work/<work id>: the run's calls, cost and receipts.\n"
        f"- GET {base}/v1/workspace/ledger: credit purchases, refunds and pending payments.\n\n"
        "## Price\n"
        f"- {BILLING_RULE}\n"
        f"- {p['free_governed_runs_per_month']} governed runs per workspace per month free "
        f"(resets 00:00 UTC on the 1st), then ${p['price_per_governed_run_usd']} per run from "
        "prepaid credits. No subscription, no auto top-up, no overage.\n"
        "- Allowance and credits used up: HTTP 402 with detail.error allowance_exhausted, "
        "before the provider is called.\n"
        "- Model spend is billed by your provider on your own key.\n"
        f"- Machine-readable: GET {base}/pricing\n\n"
        "## Buying credits\n"
        f"{buy}\n"
        "## Errors and retries\n"
        "- 400: missing provider key, bad budget header or conflicting ceiling. Fix the request.\n"
        "- 401: missing or invalid workspace key.\n"
        "- 402: run ceiling (BudgetExceededError) or allowance_exhausted. Not retryable as-is.\n"
        "- 413: request body too large.\n"
        "- 429: workspace rate limit; wait for the Retry-After seconds.\n"
        "- 502/504: provider error or timeout; error.remediation says whether to retry.\n\n"
        "## Free alternative\n"
        "The open-source gateway does the same per-run enforcement on one machine: "
        "pip install inferrail. Hosted is for many agents or machines sharing one budget "
        "ledger without running it yourself.\n\n"
        f"OpenAPI: {base}/openapi.json\n"
    )


def _next_month_utc(now: float | None = None) -> str:
    """When the free allowance resets: 00:00 UTC on the 1st of next month (ISO 8601)."""
    t = time.gmtime(time.time() if now is None else now)
    year, month = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
    return f"{year:04d}-{month:02d}-01T00:00:00Z"


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

    def _within_daily_sales_cap(cents: int) -> None:
        """Bounded experiment. Two separate limits on refund exposure:
        - daily: confirmed plus pending sales in the last 24 hours;
        - outstanding: the value of all unspent prepaid credits plus pending purchases."""
        sold = ledger.sales_cents_since(time.time() - 86_400)
        if sold + cents > int(settings.max_daily_sales_usd * 100):
            raise HTTPException(503, "daily credit sales limit reached; try again tomorrow")
        owed = ledger.outstanding_liability_cents()
        if owed + cents > int(settings.max_outstanding_credits_usd * 100):
            raise HTTPException(503, "credit sales are paused: outstanding prepaid credit limit")

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

    app.add_middleware(_BodyLimit, max_bytes=settings.max_body_bytes)
    calls: dict[str, deque[float]] = defaultdict(deque)

    # -- published terms ---------------------------------------------------------------------------
    def price_sheet() -> dict[str, Any]:
        rails: dict[str, Any] = {}
        if settings.x402_pay_to:
            rails["x402"] = {
                "endpoint": f"POST {settings.mount_prefix}/v1/credits/x402",
                "url": settings.public_base_url + "/v1/credits/x402",
                "pay_to": settings.x402_pay_to,
                "network": settings.x402_network,
                "asset": "USDC",
                "price_usd": settings.x402_pack_price_usd,
                "governed_runs": settings.x402_pack_runs,
                "account_needed": False,
            }
        if settings.stripe:
            rails["card"] = {
                "endpoint": f"POST {settings.mount_prefix}/v1/credits/checkout",
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
            "credit_purchases_open": bool(rails),
            "per_run_budget_max_usd": str(settings.per_work_max_usd),
            "api_base_url": settings.public_base_url + "/v1",
            "docs": {
                "llms_txt": settings.public_base_url + "/llms.txt",
                "openapi": settings.public_base_url + "/openapi.json",
            },
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
        return _agent_guide(price_sheet(), settings)

    # -- workspaces -------------------------------------------------------------------------------
    @app.post("/v1/workspaces", status_code=201)
    async def create_workspace(request: Request) -> dict[str, Any]:
        if not settings.creation_enabled:
            raise HTTPException(503, "workspace creation is paused")
        if ledger.count_workspaces() >= settings.max_workspaces:
            raise HTTPException(503, "workspace capacity reached")
        ip = (
            request.headers.get(settings.client_ip_header, "").strip()
            if settings.client_ip_header
            else ""
        ) or (request.client.host if request.client else "unknown")
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
        # `Authorization: Bearer` (OpenAI SDKs, curl) or `x-api-key` (the Anthropic SDK's
        # default for `api_key=`). The provider key never travels in either.
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        token = token or request.headers.get("x-api-key", "").strip()
        workspace_id = ledger.authenticate(token) if token else None
        if workspace_id is None:
            raise HTTPException(
                401,
                "missing or invalid workspace key: send it as 'Authorization: Bearer <key>' or "
                "'x-api-key: <key>'. Create one with POST /v1/workspaces",
            )
        now = time.monotonic()
        window = calls[workspace_id]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= settings.rate_limit_per_minute:
            retry_after = max(1, int(60 - (now - window[0])) + 1)
            raise HTTPException(
                429,
                f"workspace rate limit reached ({settings.rate_limit_per_minute}/minute); retry "
                f"after {retry_after}s",
                headers={"Retry-After": str(retry_after)},
            )
        window.append(now)
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
                remediation=_HOSTED_REMEDIATION.get(code.code, code.remediation),
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
            rails = price_sheet()["credits"]
            how = (
                "Buy credits to continue (see 'purchase')"
                if rails
                else "Credit purchases are not open on this deployment yet"
            )
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "allowance_exhausted",
                    "message": "This month's free governed runs are used and no credits remain. "
                    f"{how}, or wait for the monthly reset. Nothing was sent to the provider.",
                    "purchase": rails,
                    "free_allowance_resets_at": _next_month_utc(),
                    "pricing_url": settings.public_base_url + "/pricing",
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
        _within_daily_sales_cap(int(packs_raw) * settings.stripe.pack_price_cents)
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
        from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
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
            f"POST {settings.mount_prefix}/v1/credits/x402": RouteConfig(
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
                # Discovery metadata for facilitators that catalog x402 resources (Bazaar).
                # The request body is empty; the workspace key travels in Authorization.
                extensions=declare_discovery_extension(
                    input={},
                    input_schema={"type": "object", "properties": {}},
                    body_type="json",
                    output=OutputConfig(
                        example={
                            "workspace_id": "ws_0123456789abcdef0123",
                            "governed_runs": settings.x402_pack_runs,
                            "status": "credits are added once the payment settles",
                        },
                        schema={
                            "type": "object",
                            "properties": {
                                "workspace_id": {"type": "string"},
                                "governed_runs": {"type": "integer"},
                                "status": {"type": "string"},
                            },
                            "required": ["workspace_id", "governed_runs", "status"],
                        },
                    ),
                ),
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
            _within_daily_sales_cap(pack_cents)
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
        workers=1,  # one process: see the deployment constraint above
    )
