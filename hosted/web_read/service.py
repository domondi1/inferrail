"""Inferrail Web Read: a paid, per-page web reader for agents over x402.

`POST /v1/page {"url": ...}` returns the page's readable content as markdown, its title, word
count, a SHA-256 of the returned content and a receipt. It costs `READ_PRICE_USD` (default
$0.002) in USDC on Base, paid with x402. **The buyer pays only for a delivered page:** the
payment settles only after the page has been fetched and converted. A refusal (robots.txt, a
non-public address, a non-text page, too large, an upstream error or timeout) returns an error
and nothing is charged.

No third-party data supplier: pages are fetched directly from the public web, honoring
robots.txt with an identifying user agent (see fetch.py). Content isn't stored; the ledger keeps
only the URL, sizes, a content hash and the payment reference.
"""

from __future__ import annotations

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
    "(robots.txt, non-public address, non-text, >2 MB, upstream error) are not charged."
)


class PageRequest(BaseModel):
    url: str = Field(..., max_length=2048, description="Public http(s) URL to read")
    request_id: str | None = Field(None, max_length=128, description="Your id, echoed back")


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
        )


class Ledger:
    """Delivered reads and their settlement. No page content is stored."""

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

    def event(self, kind: str, code: str = "") -> None:
        with self._lock:
            self._db.execute("INSERT INTO events VALUES (?,?,?)", (kind, code, time.time()))

    def delivered(self, ref: str, payer: str, row: dict[str, Any], price: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO reads VALUES (?,?,?,?,?,?,?,?,?,0)",
                (ref, payer, row["url"], row["final_url"], row["bytes"], row["word_count"],
                 row["content_sha256"], price, time.time()),
            )  # fmt: skip

    def settled(self, ref: str) -> None:
        with self._lock:
            self._db.execute("UPDATE reads SET settled=1 WHERE payment_ref=?", (ref,))

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
        }


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

    @app.middleware("http")
    async def _x402(request: Request, call_next: Any) -> Any:
        response = await gate(request, call_next)
        if response.status_code == 402 and not request.headers.get("payment-signature"):
            ledger.event("challenge")
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
        if ref:
            ledger.delivered(ref[0], ref[1], row, settings.price_usd)
        row["receipt"] = {
            "payment_ref": ref[0] if ref else None,
            "price_usd": settings.price_usd,
            "request_id": body.request_id,
        }
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
