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
import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, WithJsonSchema

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
mpp_rail = _sibling("mpp_rail")
commerce_events = _sibling("commerce_events")

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
    "with word count, a content hash and a receipt. Pay only for a delivered page: a blocked, "
    "missing or failing page (target-site 403, 404, 5xx, timeout) costs nothing, and neither do "
    "refusals (robots.txt, non-public address, non-text, >2 MB). A retry from the same wallet "
    "never pays twice."
)

# Short form for the MPP challenge (an HTTP header): what the page is and how it's charged.
MPP_DESCRIPTION = (
    "Read one public web page as clean markdown with a content hash and receipt. Charged "
    "only when the page is delivered: a 403, 404 or 5xx from the site costs nothing, and a "
    "retry never pays twice."
)


class PageRequest(BaseModel):
    url: str = Field(..., max_length=2048, description="Public http(s) URL to read")
    # Published as a plain optional string: a catalog that reads the request contract from the
    # schema (required fields, types) read `anyOf: [string, null]` as a partial contract.
    request_id: Annotated[str | None, WithJsonSchema({"type": "string", "maxLength": 128})] = Field(
        None,
        max_length=128,
        description="Your id, echoed back. Retrying with the same url and request_id within the "
        "recovery window returns the paid page without a second charge.",
    )


@dataclass(frozen=True)
class Task:
    """What one paid request buys. The payment flow around it is the same for every task: x402
    and MPP offers, validate then deliver then settle, recovery of a paid result without a second
    charge, and the funnel. Only these parts differ (Web Read's page is the default task)."""

    path: str  # route under the mount prefix, e.g. "/v1/page"
    request_model: type[BaseModel]
    service_name: str
    description: str  # x402 offer and discovery text
    mpp_description: str  # short form for the MPP challenge header
    tags: list[str]
    input_example: dict[str, Any]
    output_example: Callable[[str], dict[str, Any]]  # price_usd -> example response
    output_schema: dict[str, Any]
    idem: Callable[[Any], str]  # retry identity of a request (never stores its content)
    deliver: Callable[[Any, Callable[[str, str], None]], Awaitable[tuple[int, dict[str, Any]]]]
    # (url, final_url, bytes, words, sha256) recorded per sale; never a request's private content
    ledger_fields: Callable[[dict[str, Any]], tuple[Any, ...]]
    guide: Callable[[Settings], str]  # /llms.txt
    # What one delivery costs us before the buyer's transfer is broadcast (a paid supplier call).
    # Above zero, a sale is held to the unpaid-cost controls in `mpp_purchase`.
    unit_cost_usd: Decimal = Decimal(0)


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
    mpp: Any = None  # mpp_rail.MppSettings when MPP (Tempo) payments are also accepted
    allow_get: bool = False  # also sell GET /v1/page?url=... (for clients and indexes that GET)
    # Also sell over x402. Off means MPP only: an EIP-3009 transfer binds a merchant and amount,
    # not an endpoint, so a second x402 service needs its own receiving address.
    x402: bool = True
    # Ceiling on supplier cost spent for transfers that then didn't settle, per 24 hours. Past it,
    # sales paid by a not-yet-broadcast transfer are refused (uncharged) until the window clears.
    unpaid_cap_usd: Decimal = Decimal("0.05")
    # Record each paid request's lifecycle in commerce_events.sqlite3 (off unless enabled).
    commerce_events: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, prefix: str = "READ_") -> Settings:
        """Settings from `<prefix>*` variables (Web Read: READ_*). x402 is on unless
        `<prefix>X402=0`."""
        e = dict(os.environ if env is None else env)
        pay_to = e.get(f"{prefix}PAY_TO", "").strip()
        if not pay_to:
            raise RuntimeError(f"{prefix}PAY_TO is required")
        network = e.get(f"{prefix}NETWORK", TESTNET)
        if network not in (MAINNET, TESTNET):
            raise RuntimeError(f"{prefix}NETWORK must be {MAINNET} or {TESTNET}")
        if network == MAINNET and e.get(f"{prefix}MAINNET_APPROVED") != "1":
            raise RuntimeError(f"mainnet sales require {prefix}MAINNET_APPROVED=1")
        price = e.get(f"{prefix}PRICE_USD", "0.002")
        recovery_s = int(e.get(f"{prefix}RECOVERY_S", "600"))
        if not 0 <= recovery_s <= 3600:
            raise RuntimeError(f"{prefix}RECOVERY_S must be between 0 and 3600")
        if not Decimal("0.001") <= Decimal(price) <= Decimal("1"):
            raise RuntimeError(f"{prefix}PRICE_USD must be between 0.001 and 1")
        resource_url = e[f"{prefix}RESOURCE_URL"]
        mpp = mpp_rail.MppSettings.from_env(e, realm=urlsplit(resource_url).netloc, prefix=prefix)
        x402 = e.get(f"{prefix}X402", "1") != "0"
        if not x402 and mpp is None:
            raise RuntimeError(f"{prefix}X402=0 needs {prefix}MPP_CHAIN_ID: nothing to sell")
        return cls(
            pay_to=pay_to,
            network=network,
            price_usd=price,
            resource_url=resource_url,
            data_dir=Path(e.get(f"{prefix}DATA_DIR", "/tmp/inferrail_web_read")),
            operator_token=e.get("BG_OPERATOR_TOKEN") or None,
            mount_prefix=e.get(f"{prefix}MOUNT_PREFIX", "").rstrip("/"),
            recovery_s=recovery_s,
            mpp=mpp,
            allow_get=e.get(f"{prefix}ALLOW_GET") == "1",
            x402=x402,
            unpaid_cap_usd=Decimal(e.get(f"{prefix}UNPAID_CAP_USD", "0.05")),
            commerce_events=e.get(f"{prefix}COMMERCE_EVENTS") == "1",
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
            -- One row per MPP credential that reached delivery: at most one delivery (and one
            -- supplier call) per credential, and the result kept for a retry of that credential.
            CREATE TABLE IF NOT EXISTS claims (
              key TEXT PRIMARY KEY, state TEXT NOT NULL, result TEXT, at REAL NOT NULL,
              idem TEXT);
            -- Delivery cost spent on a transfer that failed (or may have failed) to settle.
            CREATE TABLE IF NOT EXISTS unpaid (
              key TEXT PRIMARY KEY, payer TEXT, cost_usd TEXT NOT NULL, state TEXT, at REAL);
            """
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(reads)")}
        for column in ("idem", "result"):  # added for retry recovery; older ledgers lack them
            if column not in columns:
                self._db.execute(f"ALTER TABLE reads ADD COLUMN {column} TEXT")
        self._db.execute("CREATE INDEX IF NOT EXISTS reads_idem ON reads (payer, idem)")

    def claim(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT state, result, idem FROM claims WHERE key=?", (key,)
            ).fetchone()
        return {"state": row[0], "result": row[1], "idem": row[2]} if row else None

    def set_claim(
        self, key: str, state: str, result: str | None = None, idem: str | None = None
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO claims (key, state, result, at, idem) VALUES (?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET state=excluded.state, "
                "result=COALESCE(excluded.result, claims.result), "
                "idem=COALESCE(excluded.idem, claims.idem)",
                (key, state, result, time.time(), idem),
            )

    def release_claim(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM claims WHERE key=?", (key,))

    def record_unpaid(self, key: str, payer: str, cost: Decimal, state: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO unpaid VALUES (?,?,?,?,?)",
                (key, payer, str(cost), state, time.time()),
            )

    def clear_unpaid(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM unpaid WHERE key=?", (key,))

    def unpaid_since(self, since: float, payer: str | None = None) -> tuple[Decimal, int]:
        """(total cost, count) of unpaid deliveries since `since`, optionally for one payer."""
        q = "SELECT cost_usd FROM unpaid WHERE at >= ?" + (" AND payer=?" if payer else "")
        with self._lock:
            rows = self._db.execute(q, (since, payer) if payer else (since,)).fetchall()
        return sum((Decimal(c) for (c,) in rows), Decimal(0)), len(rows)

    def event(self, kind: str, code: str = "") -> None:
        with self._lock:
            self._db.execute("INSERT INTO events VALUES (?,?,?)", (kind, code, time.time()))

    def delivered(
        self, ref: str, payer: str, fields: tuple[Any, ...], price: str, idem: str,
        result: str | None,
    ) -> None:  # fmt: skip
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO reads (payment_ref, payer, url, final_url, bytes, words, "
                "sha256, price_usd, created_at, settled, idem, result) "
                "VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
                (ref, payer, *fields, price, time.time(), idem, result),
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
        stages = (
            "challenge", "payment_attempt", "payment_rejected", "recovered", "refused",
            "refund_owed",
        )  # fmt: skip
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, code, COUNT(*) FROM events WHERE kind IN (?,?,?,?,?,?) "
                "GROUP BY kind, code",
                stages,
            ).fetchall()
            settled = self._db.execute("SELECT COUNT(*) FROM reads WHERE settled=1").fetchone()
            failed = dict(self._db.execute(
                "SELECT code, COUNT(*) FROM events WHERE kind='attempt_failed' GROUP BY code"
            ).fetchall())  # fmt: skip
        totals = dict.fromkeys(stages, 0)
        by_client: dict[str, dict[str, int]] = {}
        for kind, code, n in rows:
            totals[kind] += n
            if kind != "refused":  # refusals are keyed by reason, not client
                by_client.setdefault(code or "unknown", {})[kind] = n
        return {
            **totals, "settled_reads": settled[0], "by_client": by_client,
            "attempt_failed": failed,
        }  # fmt: skip

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


def _reason(rail: str, text: Any) -> str:
    """`rail:reason` for a failed payment attempt. The reason is a protocol error code or an
    exception class name, folded to [a-z0-9_] and capped, so no payload can end up in it."""
    folded = "".join(ch if ch.isascii() and ch.isalnum() else "_" for ch in str(text or "").lower())
    return f"{rail}:{'_'.join(filter(None, folded.split('_')))[:48] or 'unknown'}"


def _x402_rejection(response: Any) -> str:
    """Why the x402 gate refused a signed payment, from its PAYMENT-REQUIRED `error` field."""
    from x402.http.utils import decode_payment_required_header

    try:
        required = decode_payment_required_header(response.headers["payment-required"])
        return _reason("x402", getattr(required, "error", None))
    except Exception:
        return _reason("x402", None)


def _client_class(user_agent: str | None) -> str:
    ua = (user_agent or "").lower()
    if not ua:
        return "none"
    return next((cls for needle, cls in _CLIENTS if needle in ua), "other")


# A site's 4xx is its answer about the page the caller chose (missing, gone, forbidden): the
# read worked and a retry won't change it, like a DNS failure or a robots.txt refusal (422).
# Request Timeout and Too Many Requests are the site failing to answer for now, so they stay
# with 5xx as upstream failures (502); our own timeout is a gateway timeout (504).
_TRANSIENT_4XX = {"408", "429"}


def refusal_status(exc: Any) -> int:
    """HTTP status for a refused read; every refusal is uncharged."""
    if exc.code == "timeout":
        return 504
    if exc.code == "fetch_failed":
        return 502
    if exc.code == "upstream_status":
        return 422 if exc.detail.startswith("4") and exc.detail not in _TRANSIENT_4XX else 502
    return 422


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


def _page_guide(settings: Settings) -> str:
    return (
        "# Inferrail Web Read\n\n"
        f"> {DESCRIPTION}\n\n"
        f'POST {settings.resource_url} with JSON {{"url": "https://..."}}.\n'
        + (
            f"Or GET {settings.resource_url}?url=...&request_id=... (same price and terms; "
            "use POST for URLs that carry secrets, since query strings can end up in logs).\n"
            if settings.allow_get
            else ""
        )
        + f"The first response is HTTP 402 (x402 v2, exact, ${settings.price_usd} USDC on "
        f"{settings.network} to {settings.pay_to}). Sign it and repeat the request with the "
        "PAYMENT-SIGNATURE header. HTTP 200 = delivered and paid; any error = not charged.\n"
        f"Lost the response? Send the same url and request_id again from the same wallet "
        f"within {settings.recovery_s // 60} minutes: you get the paid page back and the new "
        "signature is not settled (no second charge).\n"
        + (
            f"Also payable with MPP on Tempo (chain {settings.mpp.chain_id}): the same 402 "
            "carries a WWW-Authenticate Payment challenge; repeat the request with "
            "Authorization: Payment <credential>. The transfer is broadcast only after the "
            "page is delivered.\n"
            if settings.mpp
            else ""
        )
        + "Errors (never charged): 422 {error: unsupported_url | non_public_address | "
        "dns_failed | disallowed_by_robots_txt | unsupported_content_type | too_large | "
        "too_many_redirects | upstream_status (the site answered 4xx: missing, gone or "
        "forbidden; detail is its status)} means retrying the same URL won't help; "
        "502 {error: upstream_status (the site answered 5xx, 408 or 429) | fetch_failed} "
        "and 504 {error: timeout} may succeed later.\n"
    )


def page_task(reader: Any) -> Task:
    """Web Read's task: fetch one public page and return it as markdown."""

    async def deliver(body: Any, event: Callable[[str, str], None]) -> tuple[int, dict[str, Any]]:
        """Fetch and convert the page: (200, row) or (422/502/504, error). Payment-agnostic."""
        try:
            got = await reader.fetch(body.url.strip())
        except fetch.Refused as exc:
            event("refused", exc.code)
            status = refusal_status(exc)
            return status, {"error": exc.code, "detail": exc.detail, "charged": False}
        text = got.body.decode(got.encoding, "replace")
        if got.content_type in ("text/html", "application/xhtml+xml"):
            title, content = extract.html_to_markdown(text, got.final_url)
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
        return 200, row

    def example(price_usd: str) -> dict[str, Any]:
        return {
            "url": "https://example.com/",
            "final_url": "https://example.com/",
            "title": "Example Domain",
            "content_markdown": "# Example Domain\n\nThis domain is for use in examples...",
            "word_count": 28,
            "content_sha256": "3f1c…",
            "fetched_at": "2026-10-16T12:00:00Z",
            "receipt": {"payment_ref": "0xpayer:0xnonce", "price_usd": price_usd},
        }

    return Task(
        path="/v1/page",
        request_model=PageRequest,
        service_name="Inferrail Web Read",
        description=DESCRIPTION,
        mpp_description=MPP_DESCRIPTION,
        tags=["web-read", "scrape", "markdown", "fetch"],
        input_example={"url": "https://example.com/"},
        output_example=example,
        output_schema=OUTPUT_SCHEMA,
        idem=lambda body: _idem(body.url, body.request_id),
        deliver=deliver,
        ledger_fields=lambda row: (
            row["url"],
            row["final_url"],
            row["bytes"],
            row["word_count"],
            row["content_sha256"],
        ),
        guide=_page_guide,
    )


def create_app(
    settings: Settings,
    *,
    facilitator: Any = None,
    fetcher: Any = None,
    tempo_rail: Any = None,
    task: Task | None = None,
    recorder: Any = None,
) -> FastAPI:
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
    from x402.http import HTTPFacilitatorClient
    from x402.http.middleware.fastapi import payment_middleware
    from x402.http.types import PaymentOption, RouteConfig
    from x402.http.utils import decode_payment_signature_header
    from x402.mechanisms.evm.exact import register_exact_evm_server
    from x402.server import x402ResourceServer

    ledger = Ledger(settings.data_dir / "web_read.sqlite3")
    reader = fetcher or fetch.Fetcher(settings.user_agent)
    task = task or page_task(reader)
    if not settings.x402 and settings.mpp is None and tempo_rail is None:
        raise ValueError("x402 is off and MPP isn't configured: nothing to sell")
    rail = tempo_rail or (
        mpp_rail.TempoRail(
            settings.mpp,
            settings.pay_to,
            settings.price_usd,
            mpp_rail.ReplayStore(settings.data_dir / "mpp_replay.sqlite3"),
            task.mpp_description,
        )
        if settings.mpp
        else None
    )
    # Commerce events: written next to the ledger, never in its way (see commerce_events.py).
    events = (
        recorder
        if recorder is not None
        else (
            commerce_events.EventStore(settings.data_dir / "commerce_events.sqlite3")
            if settings.commerce_events
            else commerce_events.NullRecorder()
        )
    )
    product, route = task.service_name, f"{settings.mount_prefix}{task.path}"
    price_atomic = int(Decimal(settings.price_usd) * 1_000_000)
    mpp_network = f"tempo:{getattr(settings.mpp, 'chain_id', '')}".rstrip(":")

    def rec(type_: str, rail_name: str, **fields: Any) -> None:
        events.record(
            type_, product=product, route=route, rail=rail_name, pay_to=settings.pay_to.lower(),
            **fields,
        )  # fmt: skip

    def purchase_of(ref: str | None) -> str | None:
        """The purchase a ledger payment_ref belongs to (MPP refs map to their credential)."""
        if not ref:
            return None
        known = getattr(events, "purchase_for", lambda _: None)(ref)
        return known or commerce_events.purchase_id("ledger", ref)

    def x402_ref(request: Request) -> tuple[str, str] | None:
        """(payer:nonce, payer) of a signed x402 request, or None."""
        header = request.headers.get("payment-signature")
        if not header:
            return None
        try:
            return _payment_ref(decode_payment_signature_header(header))
        except Exception:
            return None

    if facilitator is None and settings.x402:
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

    async def after_settle(context: Any) -> None:
        ref = _payment_ref(context.payment_payload)
        if ref and getattr(context.result, "success", False):
            ledger.settled(ref[0])
            try:
                amount = int(getattr(context.result, "amount", None) or price_atomic)
            except (TypeError, ValueError):
                amount = price_atomic
            rec(
                "settled", "x402", purchase_id=commerce_events.purchase_id("ledger", ref[0]),
                ledger_key=ref[0], payer=ref[1], amount_atomic=amount, asset="USDC",
                tx_hash=getattr(context.result, "transaction", None) or None,
                network=getattr(context.result, "network", None) or settings.network,
                outcome="settled",
            )  # fmt: skip

    if settings.x402:
        server = x402ResourceServer(facilitator)
        register_exact_evm_server(server, networks=settings.network)
        server.on_after_settle(after_settle)
    example = task.output_example(settings.price_usd)

    def offer(method: str) -> Any:
        body = {"body_type": "json"} if method == "POST" else {}  # GET takes the query string
        return RouteConfig(
            accepts=PaymentOption(
                scheme="exact",
                pay_to=settings.pay_to,
                price=f"${settings.price_usd}",
                network=settings.network,
            ),
            resource=settings.resource_url,
            description=task.description,
            mime_type="application/json",
            service_name=task.service_name,
            tags=task.tags,
            extensions=declare_discovery_extension(
                input=task.input_example,
                input_schema=task.request_model.model_json_schema(),
                output=OutputConfig(example=example, schema=task.output_schema),
                **body,
            ),
        )

    methods = ("POST", "GET") if settings.allow_get else ("POST",)
    gate = (
        payment_middleware(
            {f"{m} {settings.mount_prefix}{task.path}": offer(m) for m in methods}, server
        )
        if settings.x402
        else None
    )
    app = FastAPI(title=task.service_name)
    app.state.commerce_events = events
    app.state.ledger_path = settings.data_dir / "web_read.sqlite3"
    in_flight: dict[tuple[str, str], tuple[asyncio.Lock, int]] = {}

    def is_page(request: Request) -> bool:
        return request.method in methods and request.url.path.endswith(task.path)

    async def page_request(request: Request) -> Any:
        """The task's request: a POST's JSON body, or a GET's query string."""
        if request.method == "GET":
            return task.request_model.model_validate(dict(request.query_params))
        return task.request_model.model_validate_json(await request.body())

    async def _recover(request: Request) -> tuple[str, str] | None:
        """(payer, idem) for a signed page request from a valid payer; None otherwise."""
        header = request.headers.get("payment-signature")
        if not is_page(request) or not header:
            return None
        try:
            body = await page_request(request)
            payer = _signed_by_payer(decode_payment_signature_header(header), settings)
        except Exception:
            return None
        return (payer, task.idem(body)) if payer else None

    @contextlib.asynccontextmanager
    async def exclusive(key: tuple[str, str]) -> Any:
        """One request per (payer, request) at a time, so a retry that arrives while the first
        is still settling waits for it and then gets its result."""
        lock, users = in_flight.get(key, (asyncio.Lock(), 0))
        in_flight[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = in_flight[key]
            if users <= 1:
                del in_flight[key]
            else:
                in_flight[key] = (lock, users - 1)

    def recovered(stored: str, client: str, rail_name: str) -> JSONResponse:
        ledger.event("recovered", client)
        body = json.loads(stored)
        ref = (body.get("receipt") or {}).get("payment_ref")
        rec("recovered", rail_name, purchase_id=purchase_of(ref), ledger_key=ref, client=client,
            outcome="returned_without_charge")  # fmt: skip
        body["receipt"]["recovered"] = True
        return JSONResponse(body, headers={"x-inferrail-recovered": "1"})

    async def mpp_challenge(request: Request, error: str | None = None) -> JSONResponse:
        headers = await rail.challenge_headers(str(request.url), request.method) if rail else []
        body = {"error": error, "charged": False} if error else {}
        response = JSONResponse(body, status_code=402)
        for k, v in headers:
            response.headers.append(k, v)
        return response

    def credential_key(credential: Any, authorization: str) -> str:
        """One key per payment: the signed transfer (or its hash), not its encoding."""
        payload = getattr(credential, "payload", None)
        if isinstance(payload, dict) and payload:
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        else:
            raw = authorization
        return hashlib.sha256(raw.encode()).hexdigest()

    def unpaid_exposure(payer: str) -> str | None:
        """Why a sale paid by a not-yet-broadcast transfer must be refused before any supplier
        spend, if it must: this payer recently left a delivery unpaid, or unpaid deliveries hit
        the 24-hour ceiling."""
        day = time.time() - 86400
        if ledger.unpaid_since(day, payer)[1]:
            return "payer_left_a_delivery_unpaid"
        spent, _ = ledger.unpaid_since(day)
        if spent + task.unit_cost_usd > settings.unpaid_cap_usd:
            return "unpaid_ceiling_reached"
        return None

    def paid(
        row: dict[str, Any], receipt: Any, payer: str, idem: str, client: str, pid: str
    ) -> Any:
        """Record a settled sale and answer with it. The buyer has paid: a ledger failure is
        recorded as an event and never withholds the result."""
        ref = f"tempo:{receipt.reference}"
        with contextlib.suppress(Exception):  # telemetry never withholds a paid result
            who = str(receipt.payer or payer).lower()
            fields = {"purchase_id": pid, "ledger_key": ref, "payer": who, "network": mpp_network,
                      "amount_atomic": price_atomic, "client": client}  # fmt: skip
            rec("settled", "mpp", tx_hash=receipt.reference, outcome="settled", **fields)
            rec("receipt_issued", "mpp", receipt_ref=ref, **fields)
        try:
            ledger.delivered(
                ref, (receipt.payer or payer).lower(), task.ledger_fields(row),
                settings.price_usd, idem, json.dumps(row) if settings.recovery_s else None,
            )  # fmt: skip
            ledger.settled(ref)
        except Exception:
            log.exception("settled sale not recorded")
            with contextlib.suppress(Exception):
                ledger.event("attempt_failed", _reason("mpp", "ledger_write_failed"))
        return JSONResponse(row, headers={"Payment-Receipt": receipt.to_payment_receipt()})

    def outcome_unknown(client: str, pid: str) -> JSONResponse:
        rec("settlement_unknown", "mpp", purchase_id=pid, client=client, network=mpp_network,
            code="mpp:settle_outcome_unknown")  # fmt: skip
        ledger.event("attempt_failed", _reason("mpp", "settle_outcome_unknown"))
        ledger.event("payment_pending", client)
        return JSONResponse(
            {
                "error": "payment_outcome_unknown",
                "charged": "unknown",
                "detail": "Your transfer may have been sent. Do not pay again: repeat this "
                "request with the same Authorization credential to get the result.",
            },
            status_code=503,
            headers={"Retry-After": "5"},
        )

    async def mpp_purchase(request: Request, client: str) -> JSONResponse:
        """MPP (Tempo) purchase: validate the credential, deliver, then broadcast.

        One credential buys at most one delivery: it is claimed before delivery, and the result
        is stored under the claim before the transfer is broadcast, so a retry of the same
        credential (after a lost response, an unknown settlement outcome or a restart) settles
        it and returns that result without delivering twice. For a task with a delivery cost
        (a paid supplier call), a not-yet-broadcast transfer is checked for funds first, and
        payers or 24-hour totals that left deliveries unpaid are refused before any spend."""
        assert rail is not None
        ledger.event("payment_attempt", client)
        try:
            body = await page_request(request)
        except Exception:
            rec("payment_rejected", "mpp", client=client, network=mpp_network,
                code=_reason("mpp", "invalid_request"))  # fmt: skip
            ledger.event("attempt_failed", _reason("mpp", "invalid_request"))
            return JSONResponse({"error": "invalid_request", "charged": False}, status_code=422)
        authorization = request.headers["authorization"]
        valid = await rail.validate(authorization, str(request.url))
        if valid is None or isinstance(valid, str):  # a str is the rail's reason for refusing
            rec("payment_rejected", "mpp", client=client, network=mpp_network,
                code=_reason("mpp", valid))  # fmt: skip
            ledger.event("payment_rejected", client)
            ledger.event("attempt_failed", _reason("mpp", valid))
            return await mpp_challenge(request, "payment_invalid")
        credential, payer, kind = valid
        idem = task.idem(body)
        key = credential_key(credential, authorization)
        pid = commerce_events.purchase_id("mpp", key)
        rkey = commerce_events.request_key(getattr(body, "request_id", None))
        mine = {"purchase_id": pid, "payer": str(payer).lower(), "network": mpp_network,
                "client": client, "request_key": rkey}  # fmt: skip
        rec("payment_attempt", "mpp", amount_atomic=price_atomic, **mine)
        async with exclusive(("credential", key)), exclusive((payer, idem)):
            claim = ledger.claim(key)
            if claim and claim["idem"] and claim["idem"] != idem:  # one payment, one request
                rec("duplicate_credential", "mpp", code="mpp:credential_already_used", **mine)
                ledger.event("attempt_failed", _reason("mpp", "credential_already_used"))
                return JSONResponse(
                    {"error": "credential_already_used", "charged": False}, status_code=409
                )
            if claim and claim["state"] == "settled":
                if claim["result"]:  # the same payment, asked again: its result, no new charge
                    return recovered(claim["result"], client, "mpp")
                rec("duplicate_credential", "mpp", code="mpp:credential_already_used", **mine)
                ledger.event("attempt_failed", _reason("mpp", "credential_already_used"))
                return JSONResponse(
                    {"error": "credential_already_used", "charged": False}, status_code=409
                )
            if claim and claim["state"] in ("failed", "delivering"):
                # Already refused, or interrupted mid-delivery: never deliver on it again.
                rec("duplicate_credential", "mpp", code=f"mpp:credential_{claim['state']}", **mine)
                ledger.event("attempt_failed", _reason("mpp", f"credential_{claim['state']}"))
                return await mpp_challenge(request, "payment_invalid")
            if claim and claim["state"] == "delivered":  # delivered, settlement pending
                row = json.loads(claim["result"])
            else:
                if kind == "transaction" and settings.recovery_s:
                    stored = ledger.recoverable(payer, idem, settings.recovery_s)
                    if stored is not None:  # the new transfer is never broadcast: no 2nd charge
                        return recovered(stored, client, "mpp")
                if task.unit_cost_usd and kind != "hash":
                    refusal = unpaid_exposure(payer)
                    if refusal is None:
                        try:
                            if not await rail.funded(credential, payer):
                                refusal = "insufficient_funds"
                        except Exception:
                            refusal = "funds_unverifiable"
                    if refusal:
                        rec("payment_rejected", "mpp", code=_reason("mpp", refusal), **mine)
                        ledger.event("payment_rejected", client)
                        ledger.event("attempt_failed", _reason("mpp", refusal))
                        status = 503 if refusal == "unpaid_ceiling_reached" else 402
                        if status == 402:
                            return await mpp_challenge(request, refusal)
                        return JSONResponse({"error": refusal, "charged": False}, status_code=503)
                rec("payment_verified", "mpp", **mine)
                rec("delivery_started", "mpp", **mine)
                ledger.set_claim(key, "delivering", idem=idem)
                status, row = await task.deliver(body, ledger.event)
                if status != 200:
                    rec("delivery_failed", "mpp", code=_reason("mpp", f"http_{status}"), **mine)
                    ledger.event("attempt_failed", _reason("mpp", f"http_{status}"))
                    if kind == "hash":  # the client already paid on chain; we owe it back
                        ledger.set_claim(key, "failed")
                        with contextlib.suppress(Exception):
                            await rail.settle(credential)
                        ledger.event("refund_owed", client)
                        rec("refund_owed", "mpp", **mine)
                        row.update(charged=True, refund="owed")
                    else:  # nothing was broadcast: the buyer may use this credential again
                        ledger.release_claim(key)
                    return JSONResponse(row, status_code=status)
                row["receipt"] = {
                    "payment_ref": None,  # set once the transfer settles
                    "price_usd": settings.price_usd,
                    "request_id": body.request_id,
                    "rail": "mpp-tempo",
                }
                ledger.set_claim(key, "delivered", json.dumps(row))  # before the broadcast
                rec("delivered", "mpp", **mine)
            try:
                receipt = await rail.settle(credential)
            except mpp_rail.OutcomeUnknown:
                if task.unit_cost_usd:
                    ledger.record_unpaid(key, payer, task.unit_cost_usd, "unknown")
                return outcome_unknown(client, pid)
            except Exception as exc:  # definitely not settled: the buyer wasn't charged
                ledger.set_claim(key, "failed")
                if task.unit_cost_usd:
                    ledger.record_unpaid(key, payer, task.unit_cost_usd, "unsettled")
                cause = exc.__cause__ if isinstance(exc, mpp_rail.Unsettled) else exc
                failure = _reason("mpp", f"settle_{type(cause).__name__}")
                rec("settlement_failed", "mpp", code=failure, **mine)
                ledger.event("payment_rejected", client)
                ledger.event("attempt_failed", _reason("mpp", f"settle_{type(cause).__name__}"))
                return await mpp_challenge(request, "settlement_failed")
            ledger.clear_unpaid(key)
            row["receipt"]["payment_ref"] = f"tempo:{receipt.reference}"
            ledger.set_claim(key, "settled", json.dumps(row) if settings.recovery_s else "")
            return paid(row, receipt, payer, idem, client, pid)

    @app.middleware("http")
    async def _x402(request: Request, call_next: Any) -> Any:
        response = await sell_or_challenge(request, call_next)
        if is_page(request):  # paid pages and one-time challenges must never be served from a cache
            response.headers["cache-control"] = "no-store"
        return response

    async def sell_or_challenge(request: Request, call_next: Any) -> Any:
        client = _client_class(request.headers.get("user-agent"))
        signed = bool(request.headers.get("payment-signature"))
        page = is_page(request)
        if rail and page and mpp_rail.is_mpp_authorization(request.headers.get("authorization")):
            ledger.expire(settings.recovery_s)
            return await mpp_purchase(request, client)
        if gate is None:  # MPP only: every unpaid request for the task gets the MPP challenge
            if not page:
                return await call_next(request)
            if signed:  # an x402 payment this service doesn't accept: nothing is settled
                rec("payment_rejected", "x402", client=client, network=settings.network,
                    code=_reason("x402", "x402_not_accepted"))  # fmt: skip
                ledger.event("payment_attempt", client)
                ledger.event("attempt_failed", _reason("x402", "x402_not_accepted"))
                ledger.event("payment_rejected", client)
                return await mpp_challenge(request, "x402_not_accepted")
            ledger.event("challenge", client)
            events.count_challenge(product, client)
            return await mpp_challenge(request)
        paying = x402_ref(request) if signed and page else None
        x402_fields: dict[str, Any] = {"client": client, "network": settings.network}
        if paying:
            x402_fields.update(
                purchase_id=commerce_events.purchase_id("ledger", paying[0]),
                ledger_key=paying[0], payer=paying[1],
            )  # fmt: skip
        if request.method == "POST" or page:
            ledger.expire(settings.recovery_s)
            if signed and page:
                rec("payment_attempt", "x402", amount_atomic=price_atomic, asset="USDC",
                    **x402_fields)  # fmt: skip
                ledger.event("payment_attempt", client)
        key = await _recover(request) if settings.recovery_s else None
        if key is None:
            response = await gate(request, call_next)
        else:
            async with exclusive(key):
                stored = ledger.recoverable(*key, settings.recovery_s)
                if stored is not None:
                    return recovered(stored, client, "x402")
                response = await gate(request, call_next)
        if signed and page and response.status_code == 402:
            ledger.event("attempt_failed", _x402_rejection(response))
        elif signed and page and response.status_code >= 400:  # a refused page or a bad body
            ledger.event("attempt_failed", _reason("x402", f"http_{response.status_code}"))
        if signed and page:
            delivered = request.scope.get("inferrail_delivered")
            if response.status_code == 200:
                rec("receipt_issued", "x402", receipt_ref=x402_fields.get("ledger_key"),
                    **x402_fields)  # fmt: skip
            elif response.status_code == 402:
                rec("settlement_failed" if delivered else "payment_rejected", "x402",
                    code=_x402_rejection(response), **x402_fields)  # fmt: skip
            else:  # the payment was verified, but nothing was delivered: not charged
                rec("delivery_failed", "x402", code=_reason("x402", f"http_{response.status_code}"),
                    **x402_fields)  # fmt: skip
        if response.status_code == 402:
            if not signed:
                events.count_challenge(product, client)
            ledger.event("payment_rejected" if signed else "challenge", client)
            if rail and page and not signed:  # offer MPP (Tempo) next to x402
                for k, v in await rail.challenge_headers(str(request.url), request.method):
                    response.headers.append(k, v)
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "network": settings.network}

    @app.get("/llms.txt", response_class=PlainTextResponse)
    async def guide() -> str:
        return task.guide(settings)

    async def page_post(body: Any, request: Request) -> JSONResponse:
        return await sell(body, request)

    page_post.__annotations__["body"] = task.request_model  # FastAPI validates the JSON body
    app.post(task.path)(page_post)

    if settings.allow_get:

        @app.get(task.path, include_in_schema=False)
        async def page_get(request: Request) -> JSONResponse:
            try:
                body = await page_request(request)
            except Exception:
                return JSONResponse({"error": "invalid_request", "charged": False}, status_code=422)
            return await sell(body, request)

    async def sell(body: Any, request: Request) -> JSONResponse:
        """Deliver a page the x402 gate has already verified payment for (settled after)."""
        header = request.headers.get("payment-signature")
        ref = _payment_ref(decode_payment_signature_header(header)) if header else None
        mine: dict[str, Any] = {
            "network": settings.network,
            "request_key": commerce_events.request_key(getattr(body, "request_id", None)),
        }
        if ref:
            mine.update(purchase_id=commerce_events.purchase_id("ledger", ref[0]),
                        ledger_key=ref[0], payer=ref[1])  # fmt: skip
        rec("payment_verified", "x402", **mine)
        rec("delivery_started", "x402", **mine)
        status, row = await task.deliver(body, ledger.event)
        if status != 200:
            return JSONResponse(row, status_code=status)
        request.scope["inferrail_delivered"] = True
        rec("delivered", "x402", amount_atomic=price_atomic, asset="USDC", **mine)
        row["receipt"] = {
            "payment_ref": ref[0] if ref else None,
            "price_usd": settings.price_usd,
            "request_id": body.request_id,
        }
        if ref:
            ledger.delivered(
                ref[0], ref[1], task.ledger_fields(row), settings.price_usd, task.idem(body),
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
