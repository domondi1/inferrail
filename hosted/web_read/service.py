"""Inferrail Web Read: a paid, per-page web reader for agents over x402.

`POST /v1/page {"url": ...}` returns the page's readable content as markdown, its title, word
count, a SHA-256 of the returned content and a receipt. It costs `READ_PRICE_USD` (default
$0.002) in USDC on Base, paid with x402. **The buyer pays only for a delivered page:** the
payment settles only after the page has been fetched and converted. A refusal (robots.txt, a
non-public address, a non-text page, too large, an upstream error or timeout) returns an error
and nothing is charged.

No third-party data supplier: pages are fetched directly from the public web, honoring
robots.txt with an identifying user agent (see fetch.py). The ledger keeps the URL, sizes, a
content hash and the payment reference.

**A retry never pays twice.** Standard x402 clients sign a fresh payment when they retry after a
lost response. So a delivered result is kept for `READ_RECOVERY_S` (default 10 minutes, then
deleted), keyed by payer, URL and `request_id`. The same wallet sending the same request in that
window, with any valid signature, gets the stored page back and that payment is never settled.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
MAINNET, TESTNET = "eip155:8453", "eip155:84532"
log = logging.getLogger("inferrail.web_read")


def _sibling(name: str) -> Any:
    key = f"web_read_{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


extract = _sibling("extract")
fetch = _sibling("fetch")

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string"},
        "final_url": {"type": "string"},
        "title": {"type": "string"},
        "content_markdown": {"type": "string"},
        "word_count": {"type": "integer"},
        "content_sha256": {"type": "string"},
        "fetched_at": {"type": "string"},
        "receipt": {
            "type": "object",
            "properties": {
                "payment_ref": {"type": "string"},
                "price_usd": {"type": "string"},
                "request_id": {"type": "string"},
            },
        },
    },
    "required": ["url", "final_url", "content_markdown", "word_count", "content_sha256"],
}
DESCRIPTION = (
    "Read one public web page as clean markdown (title, main text, headings, lists, links), "
    "with word count, a content hash and a receipt. Pay only for a delivered page: refusals "
    "(robots.txt, non-public address, non-text, >2 MB, upstream error) are not charged. "
    "A retry from the same wallet never pays twice."
)


class PageRequest(BaseModel):
    url: str = Field(..., max_length=2048, description="Public http(s) URL to read")
    request_id: str | None = Field(
        None,
        max_length=128,
        description="Your id, echoed back. Retrying with the same url and request_id within the "
        "recovery window returns the paid page without a second charge.",
    )


@dataclass(frozen=True)
class Settings:
    pay_to: str
    network: str = TESTNET
    price_usd: str = "0.002"
    resource_url: str = "http://localhost:8425/v1/page"
    data_dir: Path = Path("/tmp/inferrail_web_read")
    user_agent: str = "InferrailReader/1.0 (+https://github.com/domondi1/inferrail)"
    operator_token: str | None = None
    mount_prefix: str = ""  # e.g. "/read" when mounted on a shared origin
    recovery_s: int = 600  # how long a delivered page can be recovered without paying again

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ if env is None else env)
        pay_to = e.get("READ_PAY_TO", "").strip()
        if not pay_to:
            raise RuntimeError("READ_PAY_TO is required")
        network = e.get("READ_NETWORK", TESTNET)
        if network not in (MAINNET, TESTNET):
            raise RuntimeError(f"READ_NETWORK must be {MAINNET} or {TESTNET}")
        if network == MAINNET and e.get("READ_MAINNET_APPROVED") != "1":
            raise RuntimeError("mainnet sales require READ_MAINNET_APPROVED=1")
        price = e.get("READ_PRICE_USD", "0.002")
        recovery_s = int(e.get("READ_RECOVERY_S", "600"))
        if not 0 <= recovery_s <= 3600:
            raise RuntimeError("READ_RECOVERY_S must be between 0 and 3600")
        if not Decimal("0.001") <= Decimal(price) <= Decimal("1"):
            raise RuntimeError("READ_PRICE_USD must be between 0.001 and 1")
        return cls(
            pay_to=pay_to,
            network=network,
            price_usd=price,
            resource_url=e["READ_RESOURCE_URL"],
            data_dir=Path(e.get("READ_DATA_DIR", "/tmp/inferrail_web_read")),
            operator_token=e.get("BG_OPERATOR_TOKEN") or None,
            mount_prefix=e.get("READ_MOUNT_PREFIX", "").rstrip("/"),
            recovery_s=recovery_s,
        )


class Ledger:
    """Delivered reads and their settlement. A delivered page is kept only for the recovery
    window, so the paying wallet can get it back after a lost response."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS reads (
              payment_ref TEXT PRIMARY KEY, payer TEXT, url TEXT, final_url TEXT,
              bytes INTEGER, words INTEGER, sha256 TEXT, price_usd TEXT,
              created_at REAL, settled INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (kind TEXT, code TEXT, at REAL);
            """
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(reads)")}
        for column in ("idem", "result"):  # added for retry recovery; older ledgers lack them
            if column not in columns:
                self._db.execute(f"ALTER TABLE reads ADD COLUMN {column} TEXT")
        self._db.execute("CREATE INDEX IF NOT EXISTS reads_idem ON reads (payer, idem)")

    def event(self, kind: str, code: str = "") -> None:
        with self._lock:
            self._db.execute("INSERT INTO events VALUES (?,?,?)", (kind, code, time.time()))

    def delivered(
        self, ref: str, payer: str, row: dict[str, Any], price: str, idem: str, result: str | None
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO reads (payment_ref, payer, url, final_url, bytes, words, "
                "sha256, price_usd, created_at, settled, idem, result) "
                "VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
                (ref, payer, row["url"], row["final_url"], row["bytes"], row["word_count"],
                 row["content_sha256"], price, time.time(), idem, result),
            )  # fmt: skip

    def expire(self, window_s: int) -> None:
        """Delete delivered pages older than the recovery window (the ledger row stays)."""
        with self._lock:
            self._db.execute(
                "UPDATE reads SET result=NULL WHERE result IS NOT NULL AND created_at < ?",
                (time.time() - window_s,),
            )

    def recoverable(self, payer: str, idem: str, window_s: int) -> str | None:
        """The stored result of a settled read of this request by this payer, if still kept."""
        self.expire(window_s)
        with self._lock:
            hit = self._db.execute(
                "SELECT result FROM reads WHERE payer=? AND idem=? AND settled=1 "
                "AND result IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                (payer, idem),
            ).fetchone()
        return hit[0] if hit else None

    def settled(self, ref: str) -> None:
        with self._lock:
            self._db.execute("UPDATE reads SET settled=1 WHERE payment_ref=?", (ref,))

    def funnel(self) -> dict[str, Any]:
        """Each stage of the purchase path, in total and by client family. An unsigned 402 is
        a price check, not a purchase attempt; only a request carrying a payment is."""
        stages = ("challenge", "payment_attempt", "payment_rejected", "recovered", "refused")
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, code, COUNT(*) FROM events WHERE kind IN (?,?,?,?,?) "
                "GROUP BY kind, code",
                stages,
            ).fetchall()
            settled = self._db.execute("SELECT COUNT(*) FROM reads WHERE settled=1").fetchone()
        totals = dict.fromkeys(stages, 0)
        by_client: dict[str, dict[str, int]] = {}
        for kind, code, n in rows:
            totals[kind] += n
            if kind != "refused":  # refusals are keyed by reason, not client
                by_client.setdefault(code or "unknown", {})[kind] = n
        return {**totals, "settled_reads": settled[0], "by_client": by_client}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            q = self._db.execute
            paid = q("SELECT COUNT(*), COUNT(DISTINCT payer) FROM reads WHERE settled=1").fetchone()
            repeat = q(
                "SELECT COUNT(*) FROM (SELECT payer FROM reads WHERE settled=1 "
                "GROUP BY payer HAVING COUNT(*) >= 2)"
            ).fetchone()[0]
            revenue = sum(Decimal(p) for (p,) in q("SELECT price_usd FROM reads WHERE settled=1"))
            by_payer = dict(
                q("SELECT payer, COUNT(*) FROM reads WHERE settled=1 GROUP BY payer").fetchall()
            )
            events = dict(q("SELECT kind || ':' || code, COUNT(*) FROM events GROUP BY 1"))
        return {
            "settled_reads": paid[0],
            "distinct_payers": paid[1],
            "repeat_payers": repeat,
            "revenue_usd": str(revenue),
            "reads_by_payer": by_payer,
            "events": events,
            "funnel": self.funnel(),
        }


# Coarse client families, from the User-Agent only (no IPs, URLs or bodies are recorded), so the
# operator can tell catalog crawlers and our own probes from agent clients that try to pay.
_CLIENTS = (
    ("x402scan", "x402scan"), ("agentcash", "agentcash"), ("coinbase", "cdp"), ("cdp", "cdp"),
    ("inferrail", "inferrail"), ("curl", "curl"), ("python", "python"), ("httpx", "python"),
    ("aiohttp", "python"), ("node", "node"), ("undici", "node"), ("axios", "node"),
    ("go-http", "go"), ("bot", "crawler"), ("spider", "crawler"), ("crawl", "crawler"),
    ("mozilla", "browser"),
)  # fmt: skip


def _client_class(user_agent: str | None) -> str:
    ua = (user_agent or "").lower()
    if not ua:
        return "none"
    return next((cls for needle, cls in _CLIENTS if needle in ua), "other")


def _idem(url: str, request_id: str | None) -> str:
    return hashlib.sha256(f"{url.strip()}\n{request_id or ''}".encode()).hexdigest()


def _signed_by_payer(payload: Any, settings: Settings) -> str | None:
    """The payer, if this is a valid EOA-signed EIP-3009 payment to us at our price.

    Checked locally (no facilitator call, nothing submitted), so a retry can prove it comes from
    the wallet that already paid without creating a second charge. Anything else (another
    merchant, a different amount, a smart-wallet signature) returns None and takes the normal
    paid path."""
    from x402.mechanisms.evm.eip712 import hash_eip3009_authorization
    from x402.mechanisms.evm.types import ExactEIP3009Payload
    from x402.mechanisms.evm.utils import get_evm_chain_id
    from x402.mechanisms.evm.verify import verify_eoa_signature

    try:
        req = payload.accepted
        evm = ExactEIP3009Payload.from_dict(payload.payload)
        auth = evm.authorization
        price_units = int(Decimal(settings.price_usd) * 10**6)
        if (
            req.network != settings.network
            or req.pay_to.lower() != settings.pay_to.lower()
            or auth.to.lower() != settings.pay_to.lower()
            or int(auth.value) != price_units
            or not evm.signature
        ):
            return None
        digest = hash_eip3009_authorization(
            auth, get_evm_chain_id(req.network), req.asset,
            (req.extra or {})["name"], (req.extra or {})["version"],
        )  # fmt: skip
        signature = bytes.fromhex(evm.signature.removeprefix("0x"))
        if verify_eoa_signature(digest, signature, auth.from_address):
            return auth.from_address.lower()
    except Exception:  # malformed or unsupported: fall through to the paid path
        return None
    return None


def _payment_ref(payload: Any) -> tuple[str, str] | None:
    auth = (getattr(payload, "payload", None) or {}).get("authorization") or {}
    if not isinstance(auth, dict) or not auth.get("nonce") or not auth.get("from"):
        return None
    payer = str(auth["from"]).lower()
    return f"{payer}:{str(auth['nonce']).lower()}", payer


def create_app(settings: Settings, *, facilitator: Any = None, fetcher: Any = None) -> FastAPI:
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
    from x402.http import HTTPFacilitatorClient
    from x402.http.middleware.fastapi import payment_middleware
    from x402.http.types import PaymentOption, RouteConfig
    from x402.http.utils import decode_payment_signature_header
    from x402.mechanisms.evm.exact import register_exact_evm_server
    from x402.server import x402ResourceServer

    ledger = Ledger(settings.data_dir / "web_read.sqlite3")
    reader = fetcher or fetch.Fetcher(settings.user_agent)
    if facilitator is None:
        if settings.network == MAINNET:
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
    register_exact_evm_server(server, networks=settings.network)

    async def after_settle(context: Any) -> None:
        ref = _payment_ref(context.payment_payload)
        if ref and getattr(context.result, "success", False):
            ledger.settled(ref[0])

    server.on_after_settle(after_settle)
    example = {
        "url": "https://example.com/",
        "final_url": "https://example.com/",
        "title": "Example Domain",
        "content_markdown": "# Example Domain\n\nThis domain is for use in examples...",
        "word_count": 28,
        "content_sha256": "3f1c…",
        "fetched_at": "2026-10-16T12:00:00Z",
        "receipt": {"payment_ref": "0xpayer:0xnonce", "price_usd": settings.price_usd},
    }
    gate = payment_middleware(
        {
            f"POST {settings.mount_prefix}/v1/page": RouteConfig(
                accepts=PaymentOption(
                    scheme="exact",
                    pay_to=settings.pay_to,
                    price=f"${settings.price_usd}",
                    network=settings.network,
                ),
                resource=settings.resource_url,
                description=DESCRIPTION,
                mime_type="application/json",
                service_name="Inferrail Web Read",
                tags=["web-read", "scrape", "markdown", "fetch"],
                extensions=declare_discovery_extension(
                    input={"url": "https://example.com/"},
                    input_schema=PageRequest.model_json_schema(),
                    body_type="json",
                    output=OutputConfig(example=example, schema=OUTPUT_SCHEMA),
                ),
            )
        },
        server,
    )
    app = FastAPI(title="Inferrail Web Read")
    in_flight: dict[tuple[str, str], tuple[asyncio.Lock, int]] = {}

    async def _recover(request: Request) -> tuple[str, str] | None:
        """(payer, idem) for a signed page request from a valid payer; None otherwise."""
        header = request.headers.get("payment-signature")
        if request.method != "POST" or not header or not request.url.path.endswith("/v1/page"):
            return None
        try:
            body = PageRequest.model_validate_json(await request.body())
            payer = _signed_by_payer(decode_payment_signature_header(header), settings)
        except Exception:
            return None
        return (payer, _idem(body.url, body.request_id)) if payer else None

    @app.middleware("http")
    async def _x402(request: Request, call_next: Any) -> Any:
        client = _client_class(request.headers.get("user-agent"))
        signed = bool(request.headers.get("payment-signature"))
        if request.method == "POST":
            ledger.expire(settings.recovery_s)
            if signed and request.url.path.endswith("/v1/page"):
                ledger.event("payment_attempt", client)
        key = await _recover(request) if settings.recovery_s else None
        if key is None:
            response = await gate(request, call_next)
        else:
            # One request per (payer, request) at a time, so a retry that arrives while the
            # first is still settling waits for it and then gets its result.
            lock, users = in_flight.get(key, (asyncio.Lock(), 0))
            in_flight[key] = (lock, users + 1)
            try:
                async with lock:
                    stored = ledger.recoverable(*key, settings.recovery_s)
                    if stored is not None:
                        ledger.event("recovered", client)
                        body = json.loads(stored)
                        body["receipt"]["recovered"] = True
                        return JSONResponse(body, headers={"x-inferrail-recovered": "1"})
                    response = await gate(request, call_next)
            finally:
                lock, users = in_flight[key]
                if users <= 1:
                    del in_flight[key]
                else:
                    in_flight[key] = (lock, users - 1)
        if response.status_code == 402:
            ledger.event("payment_rejected" if signed else "challenge", client)
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "network": settings.network}

    @app.get("/llms.txt", response_class=PlainTextResponse)
    async def guide() -> str:
        return (
            "# Inferrail Web Read\n\n"
            f"> {DESCRIPTION}\n\n"
            f'POST {settings.resource_url} with JSON {{"url": "https://..."}}.\n'
            f"The first response is HTTP 402 (x402 v2, exact, ${settings.price_usd} USDC on "
            f"{settings.network} to {settings.pay_to}). Sign it and repeat the request with the "
            "PAYMENT-SIGNATURE header. HTTP 200 = delivered and paid; any error = not charged.\n"
            f"Lost the response? Send the same url and request_id again from the same wallet "
            f"within {settings.recovery_s // 60} minutes: you get the paid page back and the new "
            "signature is not settled (no second charge).\n"
            "Errors: 422 {error: unsupported_url | non_public_address | dns_failed | "
            "disallowed_by_robots_txt | unsupported_content_type | too_large | "
            "too_many_redirects}, 502 {error: upstream_status | fetch_failed | timeout}.\n"
        )

    @app.post("/v1/page")
    async def page(body: PageRequest, request: Request) -> JSONResponse:
        header = request.headers.get("payment-signature")
        ref = _payment_ref(decode_payment_signature_header(header)) if header else None
        try:
            got = await reader.fetch(body.url.strip())
        except fetch.Refused as exc:
            ledger.event("refused", exc.code)
            status = 502 if exc.code in ("upstream_status", "fetch_failed", "timeout") else 422
            return JSONResponse(
                {"error": exc.code, "detail": exc.detail, "charged": False}, status_code=status
            )
        text = got.body.decode(got.encoding, "replace")
        if got.content_type in ("text/html", "application/xhtml+xml"):
            title, content = extract.html_to_markdown(text)
        else:
            title, content = "", text.strip()
        row = {
            "url": body.url,
            "final_url": got.final_url,
            "title": title,
            "content_markdown": content,
            "word_count": len(content.split()),
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "bytes": len(got.body),
        }
        row["receipt"] = {
            "payment_ref": ref[0] if ref else None,
            "price_usd": settings.price_usd,
            "request_id": body.request_id,
        }
        if ref:
            ledger.delivered(
                ref[0], ref[1], row, settings.price_usd, _idem(body.url, body.request_id),
                json.dumps(row) if settings.recovery_s else None,
            )  # fmt: skip
        return JSONResponse(row)

    @app.get("/v1/operator/stats", include_in_schema=False)
    async def stats(request: Request) -> JSONResponse:
        import hmac

        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not settings.operator_token:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if not hmac.compare_digest(token.encode(), settings.operator_token.encode()):
            return JSONResponse({"detail": "operator token required"}, status_code=401)
        return JSONResponse(json.loads(json.dumps(ledger.stats(), default=str)))

    return app
