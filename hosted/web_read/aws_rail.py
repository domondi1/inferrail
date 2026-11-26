"""AWS Marketplace Agentic Commerce for a Web Read task: agents pay per page on their AWS bill.

Off unless `<prefix>AWS_PRODUCT_ID` is set (and `<prefix>AWS_APPROVED=1`). When on, the task is
also sold at `<mount_prefix>/aws<task.path>` (for Web Read: `/read/aws/v1/page`) through x402's
`exact` scheme on network `aws:base`, using AWS's seller SDK (`fiat-extension-for-x402`): every
402 carries a quote signed with our own key, the buyer's agent gets a payment token for it from
AWS Marketplace (AgentCore payments), and we verify that token before delivering.

Two things differ from our USDC rails, and this module handles both:

- **AWS captures the payment when it issues the token**, before we deliver. To keep the promise
  that a page which can't be read is never charged, the *unpaid* request is read first: an
  unreadable page is answered with its free refusal (no quote, so nothing can be paid), and a
  readable one is held for `HOLD_S` seconds so the paid retry is served that same page. A paid
  request that still fails (the hold expired and the page went away) is recorded as
  `aws_refund_due` for a billing adjustment, and the buyer is told so.
- **Verification is stateless**, so a payment token is valid until it expires. Each token's
  `payment_id` (from the verified token, never from the buyer's payload) is claimed once in a
  SQLite store; a retry with the same payment gets the stored result back instead of a second
  delivery, and DPoP proofs are checked against a shared nonce store.

Nothing here touches the USDC (x402 on Base) or MPP (Tempo) rails: the routes, resource server and
stores are separate, and the main Web Read middleware passes `/aws/` paths straight through.
"""

from __future__ import annotations

import contextvars
import json
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

NETWORK = "aws:base"
HOLD_S = 600  # how long a page read for an unpaid request waits for its paid retry
HOLD_MAX = 2000  # pages held at once; the oldest is dropped first
PREFETCH_PER_MINUTE = 120  # unpaid reads per minute; above it the quote is sent without a read
QUOTE_REFERENCE = "inferrail-web-read-page-v1"
_PRODUCT_ID = re.compile(r"prod-[a-z0-9]{6,64}")


@dataclass(frozen=True)
class AwsSettings:
    product_id: str  # the AWS Marketplace product ID: the quote's payTo and settlement.product_id
    key_pem: bytes  # private quote-signing key (EC P-256/384/521 or RSA >= 2048), never logged
    key_id: str  # `kid` of the published key
    service_url: str  # quote `iss` and the seller URL registered for the listing
    payment_token_jwks_url: str | None = None  # None = AWS's production payment service

    @classmethod
    def from_env(cls, env: dict[str, str], prefix: str, resource_url: str,
                 mount_prefix: str) -> AwsSettings | None:  # fmt: skip
        product_id = env.get(f"{prefix}AWS_PRODUCT_ID", "").strip()
        if not product_id:
            return None
        if env.get(f"{prefix}AWS_APPROVED") != "1":
            raise RuntimeError(f"AWS Agentic Commerce sales require {prefix}AWS_APPROVED=1")
        if not _PRODUCT_ID.fullmatch(product_id):
            raise RuntimeError(f"{prefix}AWS_PRODUCT_ID must look like prod-xxxxxxxxxxxxx")
        pem = env.get(f"{prefix}AWS_QUOTE_KEY_PEM", "").strip()
        key_file = env.get(f"{prefix}AWS_QUOTE_KEY_FILE", "").strip()
        if key_file:
            pem = Path(key_file).read_text().strip()
        if "PRIVATE KEY" not in pem:
            raise RuntimeError(
                f"{prefix}AWS_PRODUCT_ID needs {prefix}AWS_QUOTE_KEY_PEM or "
                f"{prefix}AWS_QUOTE_KEY_FILE"
            )
        parts = urlsplit(resource_url)
        return cls(
            product_id=product_id,
            key_pem=pem.encode(),
            key_id=env.get(f"{prefix}AWS_KEY_ID", "inferrail-quote-1").strip()
            or "inferrail-quote-1",
            service_url=f"{parts.scheme}://{parts.netloc}{mount_prefix}/aws",
            payment_token_jwks_url=env.get(f"{prefix}AWS_PAYMENT_TOKEN_JWKS_URL") or None,
        )


class PaymentStore:
    """Each AWS payment is used once: claimed after verification, then holds its result."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._db.executescript(
            "PRAGMA journal_mode=WAL;"
            "CREATE TABLE IF NOT EXISTS aws_payments (payment_id TEXT PRIMARY KEY, claimed_at REAL,"
            " status TEXT, http_status INTEGER, response TEXT);"
            "CREATE TABLE IF NOT EXISTS aws_dpop (jti TEXT PRIMARY KEY, expires_at REAL);"
        )

    def claim(self, payment_id: str) -> bool:
        """True for the first claim of a payment, False for every later one."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO aws_payments (payment_id, claimed_at, status) "
                "VALUES (?,?,?)",
                (payment_id, time.time(), "claimed"),
            )
            return cur.rowcount == 1

    def finish(self, payment_id: str, status: str, http_status: int, body: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE aws_payments SET status=?, http_status=?, response=? WHERE payment_id=?",
                (status, http_status, json.dumps(body), payment_id),
            )

    def result(self, payment_id: str) -> tuple[str, int | None, dict[str, Any] | None]:
        row = self._db.execute(
            "SELECT status, http_status, response FROM aws_payments WHERE payment_id=?",
            (payment_id,),
        ).fetchone()
        if row is None:
            return "unknown", None, None
        return row[0], row[1], json.loads(row[2]) if row[2] else None

    # DPoP nonce store (fiat_extension_x402.DPoPNonceStore): has(jti) / add(jti, expires_at)
    def has(self, jti: str) -> bool:
        with self._lock:
            self._db.execute("DELETE FROM aws_dpop WHERE expires_at < ?", (time.time(),))
            return (
                self._db.execute("SELECT 1 FROM aws_dpop WHERE jti=?", (jti,)).fetchone()
                is not None
            )

    def add(self, jti: str, expires_at: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO aws_dpop VALUES (?,?)", (jti, float(expires_at))
            )


class Hold:
    """Pages read for unpaid requests, kept briefly for their paid retry (bounded, oldest first)."""

    def __init__(self, ttl_s: int = HOLD_S, size: int = HOLD_MAX) -> None:
        self._rows: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._ttl, self._size = ttl_s, size

    def put(self, key: str, row: dict[str, Any]) -> None:
        self._rows.pop(key, None)
        self._rows[key] = (time.monotonic(), row)
        while len(self._rows) > self._size:
            self._rows.popitem(last=False)

    def take(self, key: str) -> dict[str, Any] | None:
        got = self._rows.pop(key, None)
        if got is None or time.monotonic() - got[0] > self._ttl:
            return None
        return got[1]


class RateLimit:
    """At most `per_minute` unpaid reads per rolling minute, across all callers."""

    def __init__(self, per_minute: int = PREFETCH_PER_MINUTE) -> None:
        self._stamps: list[float] = []
        self._per_minute = per_minute

    def allow(self) -> bool:
        now = time.monotonic()
        self._stamps = [t for t in self._stamps if now - t < 60]
        if len(self._stamps) >= self._per_minute:
            return False
        self._stamps.append(now)
        return True


def _payment_id(payload: Any) -> str | None:
    """`payment_id` of the payment token in a payload the facilitator has verified."""
    import jwt

    token = (getattr(payload, "payload", None) or {}).get("paymentToken")
    if not isinstance(token, str):
        return None
    try:
        claims = jwt.decode(token, options={"verify_signature": False})  # verified just before
    except jwt.PyJWTError:
        return None
    pid = claims.get("payment_id")
    return pid if isinstance(pid, str) and pid else None


def build(
    *,
    task: Any,
    aws: AwsSettings,
    price_usd: str,
    data_dir: Path,
    mount_prefix: str,
    allow_get: bool,
    event: Callable[[str, str], None],
    rec: Callable[..., None],
    verifier: Any = None,
) -> FastAPI:
    """The AWS sub-app (mount it at "/aws"). `event` is the Web Read ledger's funnel counter and
    `rec` its commerce-event recorder; `verifier` is for tests (default: AWS's payment service)."""
    from fiat_extension_x402 import (
        AgenticCommerceVerifier,
        LocalQuoteSigner,
        StaticSellerKeyResolver,
        jwks_document,
        register_exact_agentic_commerce_facilitator,
        register_exact_agentic_commerce_server,
    )
    from x402 import x402Facilitator, x402ResourceServer
    from x402.http import PaymentOption
    from x402.http.middleware.fastapi import payment_middleware
    from x402.http.types import RouteConfig

    signer = LocalQuoteSigner.from_pem(aws.key_pem, kid=aws.key_id, service_url=aws.service_url)
    store = PaymentStore(data_dir / "aws_payments.sqlite3")
    verifier = verifier or AgenticCommerceVerifier(
        seller_key_resolver=StaticSellerKeyResolver(signer.public_jwk),
        payment_token_jwks_url=aws.payment_token_jwks_url,
    )
    facilitator = register_exact_agentic_commerce_facilitator(x402Facilitator(), verifier=verifier)
    server = register_exact_agentic_commerce_server(
        x402ResourceServer(facilitator), signer=signer, dpop_nonce_store=store
    )
    # (payment_id, first claim?) of the verified payment on this request, set by the hook below.
    claimed: contextvars.ContextVar[tuple[str, bool] | None] = contextvars.ContextVar(
        "inferrail_aws_claim", default=None
    )

    async def claim_payment(context: Any) -> None:
        # Registered after the SDK's own DPoP check, so a refused proof never claims a payment.
        pid = _payment_id(context.payment_payload)
        if pid:
            claimed.set((pid, store.claim(pid)))

    server.on_after_verify(claim_payment)

    path = f"{mount_prefix}/aws{task.path}"
    methods = ("POST", "GET") if allow_get else ("POST",)
    route = RouteConfig(
        accepts=PaymentOption(
            scheme="exact",
            network=NETWORK,
            pay_to=aws.product_id,
            price=f"${price_usd}",
            extra={"description": task.service_name, "reference": QUOTE_REFERENCE},
        ),  # fmt: skip
        description=task.description,
        mime_type="application/json",
        service_name=task.listing_name or task.service_name,
    )
    gate = payment_middleware({f"{m} {path}": route for m in methods}, server)
    hold, limit = Hold(), RateLimit()
    app = FastAPI(title=f"{task.service_name} (AWS Marketplace)")
    app.state.aws_store = store

    async def body_of(request: Request) -> Any:
        if request.method == "GET":
            return task.request_model.model_validate(dict(request.query_params))
        return task.request_model.model_validate_json(await request.body())

    def is_page(request: Request) -> bool:
        return request.method in methods and request.url.path.endswith(task.path)

    @app.middleware("http")
    async def sell(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        if not is_page(request):
            return await call_next(request)
        paying = bool(request.headers.get("payment-signature") or request.headers.get("x-payment"))
        if not paying:
            raw = await request.body() if request.method == "POST" else b"x"
            if request.method == "POST" and not raw.strip():
                event("aws", "challenge")  # a bare probe (catalog validators) gets the quote
                return await gate(request, call_next)
            try:
                body = await body_of(request)
            except Exception:
                event("aws", "invalid_request")
                return JSONResponse({"error": "invalid_request", "charged": False}, status_code=422)
            if limit.allow():
                status, row = await task.deliver(body, event)
                if status != 200:  # unreadable: refused for free, and no quote is ever issued
                    event("aws", "refused_before_quote")
                    return JSONResponse(row, status_code=status)
                hold.put(task.idem(body), row)
            else:
                event("aws", "quote_without_read")
            event("aws", "challenge")
            response = await gate(request, call_next)
            response.headers["cache-control"] = "no-store"
            return response
        event("aws", "payment_attempt")
        response = await gate(request, call_next)
        response.headers["cache-control"] = "no-store"
        if response.status_code == 402:
            event("aws", "payment_rejected")
        return response

    async def deliver(body: Any, request: Request) -> JSONResponse:
        got = claimed.get()
        if got is None:  # verified without a payment id: never deliver unpaid
            return JSONResponse({"error": "payment_unverified", "charged": False}, status_code=402)
        pid, first = got
        ref = f"aws:{pid}"
        if not first:
            status, http_status, stored = store.result(pid)
            if stored is not None and http_status is not None:
                event("aws", "recovered")
                rec("recovered", "aws", ledger_key=ref, outcome="returned_without_charge")
                stored = {**stored, "receipt": {**(stored.get("receipt") or {}), "recovered": True}}
                return JSONResponse(stored, status_code=http_status,
                                    headers={"x-inferrail-recovered": "1"})  # fmt: skip
            return JSONResponse({"error": "payment_in_progress", "payment_ref": ref,
                                 "charged": True,
                                 "detail": "this payment is being delivered; retry shortly"},
                                status_code=409)  # fmt: skip
        rec("payment_verified", "aws", ledger_key=ref, network=NETWORK)
        row = hold.take(task.idem(body))
        status = 200
        if row is None:
            status, row = await task.deliver(body, event)
        if status != 200:
            # Paid (AWS captured it) but nothing to deliver: owed back as a billing adjustment.
            event("aws", "refund_due")
            rec("delivery_failed", "aws", ledger_key=ref, network=NETWORK, code="aws:refund_due")
            failed = {**row, "charged": True, "refund_due": True, "payment_ref": ref}
            store.finish(pid, "refund_due", 502, failed)
            return JSONResponse(failed, status_code=502)
        row = {**row, "receipt": {"payment_ref": ref, "price_usd": price_usd,
                                  "request_id": getattr(body, "request_id", None),
                                  "rail": "aws"}}  # fmt: skip
        row.pop("bytes", None)
        store.finish(pid, "delivered", 200, row)
        event("aws", "delivered")
        rec("delivered", "aws", ledger_key=ref, network=NETWORK, outcome="delivered")
        return JSONResponse(row)

    async def page_post(body: Any, request: Request) -> JSONResponse:
        return await deliver(body, request)

    page_post.__annotations__["body"] = task.request_model
    app.post(task.path)(page_post)
    if allow_get:

        @app.get(task.path, include_in_schema=False)
        async def page_get(request: Request) -> JSONResponse:
            try:
                body = await body_of(request)
            except Exception:
                return JSONResponse({"error": "invalid_request", "charged": False}, status_code=422)
            return await deliver(body, request)

    @app.get("/.well-known/jwks.json")
    async def jwks() -> dict[str, Any]:
        """The public quote-signing key AWS Marketplace verifies our quotes with."""
        return jwks_document(signer.public_jwk)

    return app
