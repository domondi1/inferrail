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

**Recover matching paid requests within the recovery window.** Standard x402 clients sign a
fresh payment when they retry after a
lost response. A delivered result supports recovery for `READ_RECOVERY_S` (default 10 minutes,
with lazy expiration cleanup), keyed by payer, URL and `request_id`. The same wallet sending the same request in that
window, with any valid signature, gets the stored page back and that payment is never settled.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import importlib.util
import json
import logging
import os
import re
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
# Extra x402 networks Web Read can also accept, each with its own receiving address. Off unless
# READ_X402_EXTRA_NETWORKS lists them; mainnet ones also need READ_X402_EXTRA_MAINNET_APPROVED=1.
# (The SDK has no default USDC for Polygon's testnet, so Polygon is mainnet only.)
POLYGON_MAINNET = "eip155:137"
SOLANA_MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOLANA_DEVNET = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
EXTRA_NETWORKS = {POLYGON_MAINNET: "evm", SOLANA_MAINNET: "svm", SOLANA_DEVNET: "svm"}
EXTRA_MAINNETS = frozenset({POLYGON_MAINNET, SOLANA_MAINNET})
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
meta = _sibling("meta")
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
        # What the page declares about itself; null (authors: []) when it doesn't say.
        "metadata": {
            "type": "object",
            "properties": {
                "description": {"type": ["string", "null"]},
                "canonical_url": {"type": ["string", "null"]},
                "authors": {"type": "array", "items": {"type": "string"}},
                "published_at": {"type": ["string", "null"]},
                "modified_at": {"type": ["string", "null"]},
                "language": {"type": ["string", "null"]},
                "site_name": {"type": ["string", "null"]},
                "image": {"type": ["string", "null"]},
                "type": {"type": ["string", "null"]},
            },
        },
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
# Echoed by the buyer in its payment and forwarded to the facilitator: it must stay within the
# facilitator's schema (500 characters, x402_contract.py; a 501-character one refused every
# verify on 2026-11-23) and within OFFER_DESCRIPTION_MAX, the longest one that settled sales.
# It is also what the CDP Bazaar's task search ranks, so it opens with the task phrase agents
# search for ("web page to Markdown", "URL"). Richer wording lives in OpenAPI and the guides.
DESCRIPTION = (
    "Web page to Markdown: read public URLs and text-layer PDFs as clean Markdown with source "
    "metadata and a content hash. Use it to quote, cite or check for changes. $0.002 per "
    "delivered page. Robots refusals, bot checks and upstream failures are free. Recover a "
    "matching paid request from the same payer within the recovery window."
)
# The longest offer description the CDP facilitator has accepted for a settled sale. Its schema
# allows 500 (x402_contract.description_limit()); a test keeps every task under both.
OFFER_DESCRIPTION_MAX = 373

# Short form for the MPP challenge (an HTTP header): what the page is and how it's charged.
MPP_DESCRIPTION = (
    "Read one public web page as clean markdown with a content hash and receipt. Charged "
    "only when the page is delivered: a 403, 404 or 5xx from the site costs nothing, and "
    "matching paid requests can be recovered within the recovery window."
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
    service_name: str  # the product name in our own events, OpenAPI and guides
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
    # How many units of the price one valid request buys (Web Read's batch: one per page). None
    # means one: the price is fixed. A task with units has its request validated before any
    # offer is made, and every offer, check, receipt and settlement is for units x price.
    units: Callable[[Any], int] | None = None
    # The output schema published in the 402's discovery extension (echoed by the buyer to the
    # facilitator). None means output_schema; either way it is made facilitator-safe first.
    discovery_schema: dict[str, Any] | None = None
    # The name discovery indexes (CDP Bazaar `serviceName`, at most 32 characters) show and rank,
    # phrased as the task agents search for. None means service_name.
    listing_name: str | None = None
    # What one paid result is called in buyer-facing text ("page", "package check", ...), and the
    # namespace of its credit holds in the workspace ledger (Web Read's existing refs stay valid).
    result_noun: str = "page"
    credits_ref: str = "web-read"


def bazaar_safe(value: Any) -> Any:
    """A schema or example as it may go in the 402's discovery extension: every JSON Schema
    `type` list becomes its first non-null type and every null value is dropped. Buyers echo the
    extension to the facilitator; the shapes accepted for every settled sale had neither."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k == "type" and isinstance(v, list):
                v = next((t for t in v if t != "null"), "string")
            if v is None:
                continue
            out[k] = bazaar_safe(v)
        return out
    if isinstance(value, list):
        return [bazaar_safe(v) for v in value if v is not None]
    return value


def unpaid_guide(settings: Any, task: Any, units: int = 1, credits: bool = False) -> dict[str, Any]:
    """The body of an unpaid 402, for callers that aren't x402 clients yet (an LLM with a plain
    HTTP tool, a developer with curl). x402 v2 puts the offer in the PAYMENT-REQUIRED header, so
    the default body is `{}`: a dead end for anyone who can't read it. This says what it costs,
    that nothing was charged, and how to start paying from a wallet the caller controls.

    It has no `x402Version` key on purpose: x402 clients read the header first and look at the
    body only for a v1 offer, so paying clients never see it, and it is never echoed to the
    facilitator."""
    from urllib.parse import urlsplit

    url = urlsplit(settings.resource_url)
    origin = f"{url.scheme}://{url.netloc}"
    pay: dict[str, str] = {
        "x402": "USDC on Base: pay the offer in this response's PAYMENT-REQUIRED header with any "
        "x402 client, then repeat this request with the PAYMENT-SIGNATURE header.",
    }
    if getattr(settings, "mpp", None) is not None:
        pay["mpp"] = "Tempo: answer the WWW-Authenticate: Payment challenge with an MPP client."
    return {
        "error": "payment_required",
        "charged": False,
        "price_usd": price_for(settings.price_usd, units),
        "service": task.listing_name or task.service_name,
        "summary": task.mpp_description,
        "charged_only_on_delivery": True,
        "pay": pay,
        "start_paying": {
            "agentcash": f"npx agentcash add {origin}",
            "vercel_ai_sdk": "npm i @inferrail/ai-sdk-tools",
            "mcp_client": "npx -y @inferrail/ai-sdk-tools (a local MCP server for Claude Code, "
            "Claude Desktop or Cursor; set INFERRAIL_WALLET_KEY and INFERRAIL_MAX_USD_PER_DAY)",
            "wallet": "Each pays from a wallet you control and fund; none creates or funds one.",
            **({"api_key": f"No wallet: POST {origin}/hosted/v1/workspaces for a key; fund it via "
                f"the methods listed in GET {origin}/hosted/pricing, then POST "
                f"{origin}{settings.mount_prefix}/credits{task.path} with 'Authorization: Bearer "
                f"irw_...' (2 credits = $0.002 per delivered {task.result_noun})."}
               if credits else {}),
        },
        "docs": {"guide": f"{origin}{settings.mount_prefix}/llms.txt",
                 "openapi": f"{origin}/openapi.json"},
        "recovery": {"window_seconds": settings.recovery_s,
                     "requires": "Same request content, request_id and payer identity; "
                                 "use a new request_id for fresh evidence."},
    }  # fmt: skip


def discovery_output(task: Any, example: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(schema, example) for the 402's discovery extension: the task's discovery schema, and the
    example reduced to that schema's top-level fields, both facilitator-safe."""
    schema = task.discovery_schema or task.output_schema
    keys = set((schema.get("properties") or {}).keys())
    trimmed = {k: v for k, v in example.items() if not keys or k in keys}
    return bazaar_safe(schema), bazaar_safe(trimmed)


def price_for(price_usd: str, units: int) -> str:
    """The price of `units` units, as a plain decimal string ("0.002" x 3 -> "0.006")."""
    return format((Decimal(price_usd) * units).normalize(), "f")


DEFAULT_ICON_URL = "https://tryinferrail.com/favicon-192x192.png"


def _extra_networks(e: dict[str, str], prefix: str, primary: str) -> tuple[tuple[str, str], ...]:
    """`<prefix>X402_EXTRA_NETWORKS` = "network=payTo,network=payTo", validated; () when unset."""
    raw = e.get(f"{prefix}X402_EXTRA_NETWORKS", "").strip()
    if not raw:
        return ()
    out: list[tuple[str, str]] = []
    for item in raw.split(","):
        network, _, pay_to = item.strip().partition("=")
        network, pay_to = network.strip(), pay_to.strip()
        if network not in EXTRA_NETWORKS:
            raise RuntimeError(
                f"{prefix}X402_EXTRA_NETWORKS: {network!r} is not supported "
                f"(one of {', '.join(sorted(EXTRA_NETWORKS))})"
            )
        if network == primary or network in dict(out):
            raise RuntimeError(f"{prefix}X402_EXTRA_NETWORKS lists {network} twice")
        if not _valid_pay_to(network, pay_to):
            raise RuntimeError(f"{prefix}X402_EXTRA_NETWORKS: invalid payTo for {network}")
        if network in EXTRA_MAINNETS and e.get(f"{prefix}X402_EXTRA_MAINNET_APPROVED") != "1":
            raise RuntimeError(
                f"{network} sales require {prefix}X402_EXTRA_MAINNET_APPROVED=1 (separate approval)"
            )
        out.append((network, pay_to))
    return tuple(out)


def _valid_pay_to(network: str, pay_to: str) -> bool:
    if EXTRA_NETWORKS.get(network) == "svm":
        from x402.mechanisms.svm.utils import validate_svm_address

        return validate_svm_address(pay_to)
    return len(pay_to) == 42 and pay_to.startswith("0x") and _is_hex(pay_to[2:])


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _icon_url(value: str, prefix: str) -> str | None:
    """An absolute https icon URL, or None when set empty (no icon in the offer)."""
    value = value.strip()
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc or len(value) > 2048:
        raise RuntimeError(f"{prefix}ICON_URL must be an absolute https URL (or empty)")
    if parts.path.lower().endswith(".svg"):
        raise RuntimeError(f"{prefix}ICON_URL can't be an SVG (discovery indexes reject it)")
    return value


@dataclass(frozen=True)
class Settings:
    pay_to: str
    network: str = TESTNET
    price_usd: str = "0.002"
    resource_url: str = "http://localhost:8425/v1/page"
    data_dir: Path = Path("/tmp/inferrail_web_read")
    user_agent: str = "InferrailReader/1.0 (+https://github.com/domondi1/inferrail)"
    operator_token: str | None = None
    # A separate, read-only reporting credential: it opens only /v1/operator/stats, without the
    # per-wallet breakdown. It is never a key seed and never opens anything else.
    stats_token: str | None = None
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
    # Service icon in the x402 offer (`resource.iconUrl`). Discovery indexes such as the CDP
    # Bazaar show it and count it toward listing quality; they accept PNG/JPEG/WebP/GIF, not SVG.
    icon_url: str | None = DEFAULT_ICON_URL
    # Also sell the page as a paid MCP tool (`read_page`) at <mount_prefix>/mcp. Off by default.
    mcp: bool = False
    # Other networks accepted next to `network`, as (network, pay_to) pairs. Empty by default.
    extra_networks: tuple[tuple[str, str], ...] = ()
    # Receiving address for x402 on `network`, when it must differ from `pay_to` (which then
    # stays the MPP recipient). An EIP-3009 transfer binds a merchant, not a route, so a second
    # x402 service needs its own address, while MPP challenges are route-bound and can share one.
    x402_pay_to: str = ""
    # Read PDFs (text layer, first 50 pages) as well as HTML and text. On unless READ_PDF=0.
    pdf: bool = True
    # AWS Marketplace Agentic Commerce (aws_rail.AwsSettings), sold at <mount_prefix>/aws<path>.
    aws: Any = None

    @property
    def x402_recipient(self) -> str:
        return self.x402_pay_to or self.pay_to

    def accepted(self) -> tuple[tuple[str, str], ...]:
        """Every (network, pay_to) this service sells on over x402, the primary one first."""
        return ((self.network, self.x402_recipient), *self.extra_networks)

    def pay_to_for(self, network: str) -> str | None:
        return dict(self.accepted()).get(network)

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
        x402_pay_to = e.get(f"{prefix}X402_PAY_TO", "").strip()
        if x402_pay_to and not x402:
            raise RuntimeError(f"{prefix}X402_PAY_TO is set but {prefix}X402=0")
        if x402_pay_to and not re.fullmatch(r"0x[0-9a-fA-F]{40}", x402_pay_to):
            raise RuntimeError(f"{prefix}X402_PAY_TO must be a 0x EVM address")
        return cls(
            pay_to=pay_to,
            network=network,
            price_usd=price,
            resource_url=resource_url,
            data_dir=Path(e.get(f"{prefix}DATA_DIR", "/tmp/inferrail_web_read")),
            operator_token=e.get("BG_OPERATOR_TOKEN") or None,
            stats_token=_stats_token(e),
            mount_prefix=e.get(f"{prefix}MOUNT_PREFIX", "").rstrip("/"),
            recovery_s=recovery_s,
            mpp=mpp,
            allow_get=e.get(f"{prefix}ALLOW_GET") == "1",
            x402=x402,
            unpaid_cap_usd=Decimal(e.get(f"{prefix}UNPAID_CAP_USD", "0.05")),
            commerce_events=e.get(f"{prefix}COMMERCE_EVENTS") == "1",
            icon_url=_icon_url(e.get(f"{prefix}ICON_URL", DEFAULT_ICON_URL), prefix),
            extra_networks=_extra_networks(e, prefix, network) if x402 else (),
            mcp=e.get(f"{prefix}MCP") == "1" and x402,
            x402_pay_to=x402_pay_to,
            pdf=e.get(f"{prefix}PDF", "1") != "0",
            aws=_aws_settings(e, prefix, resource_url),
        )


def _aws_settings(e: dict[str, str], prefix: str, resource_url: str) -> Any:
    """AWS Agentic Commerce settings, or None. Fail-isolated: a bad AWS configuration is logged and
    leaves only the AWS rail off, never Web Read itself (its USDC and MPP rails are unaffected)."""
    if not e.get(f"{prefix}AWS_PRODUCT_ID", "").strip():
        return None
    try:
        return _sibling("aws_rail").AwsSettings.from_env(
            e, prefix, resource_url, e.get(f"{prefix}MOUNT_PREFIX", "").rstrip("/")
        )
    except Exception:
        log.exception("AWS Agentic Commerce rail not enabled: configuration error")
        return None


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

    def result_for(self, ref: str) -> tuple[bool, str | None]:
        """(this payment already reached delivery, its stored result if still kept)."""
        with self._lock:
            row = self._db.execute(
                "SELECT result FROM reads WHERE payment_ref=?", (ref,)
            ).fetchone()
        return (row is not None, row[0] if row else None)

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
        with self._lock:
            channels = dict(self._db.execute(
                "SELECT code, COUNT(*) FROM events WHERE kind='channel' GROUP BY code"
            ).fetchall())  # fmt: skip
        return {
            **totals, "settled_reads": settled[0], "by_client": by_client,
            "attempt_failed": failed, "by_channel": channels,
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


# Where a request came from, when the caller says so with `X-Inferrail-Source`: the integration
# surface (one of these fixed labels), never a buyer's identity. Anything else is ignored. It lets
# the funnel tell which distribution channel leads to price checks, payments and deliveries.
SOURCES = frozenset({"skill", "plugin", "example", "llms", "ci", "ai-sdk", "mcp-local"})


def _source(request: Any) -> str | None:
    value = (request.headers.get("x-inferrail-source") or "").strip().lower()
    return value if value in SOURCES else None


def _stats_token(e: dict[str, str]) -> str | None:
    """INFERRAIL_STATS_TOKEN, when it is long enough and isn't the operator token (sharing the
    operator token would defeat the point of a narrow credential)."""
    token = (e.get("INFERRAIL_STATS_TOKEN") or "").strip()
    if len(token) < 32 or token == (e.get("BG_OPERATOR_TOKEN") or "").strip():
        return None
    return token


def _reason(rail: str, text: Any) -> str:
    """`rail:reason` for a failed payment attempt. The reason is a protocol error code or an
    exception class name, folded to [a-z0-9_] and capped, so no payload can end up in it."""
    folded = "".join(ch if ch.isascii() and ch.isalnum() else "_" for ch in str(text or "").lower())
    return f"{rail}:{'_'.join(filter(None, folded.split('_')))[:48] or 'unknown'}"


# The facilitator's own explanation of the last refused payment in this request (its message and
# correlation id), kept for the operator's private event record. Never sent to the buyer.
_facilitator_note: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "inferrail_facilitator_note", default=None
)
_FACILITATOR_ERROR = re.compile(r"Facilitator verify failed \((\d{3})\): (.*)", re.S)
_CORRELATION = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class ExplainedFacilitator:
    """The configured facilitator, with an error answer to `verify` turned into a refused
    verification that carries the facilitator's own reason.

    The CDP facilitator answers an invalid payment (an unfunded wallet, an expired
    authorization) with HTTP 400 and a JSON body `{invalidReason, invalidMessage, payer,
    correlationId}`. The SDK raises that as an exception, which reached the buyer as a raw string
    and was recorded truncated. Here a 4xx becomes `is_valid=False` with `invalidReason`, so the
    buyer's client gets the standard x402 reason; a 5xx or transport error becomes
    `facilitator_unavailable`. It only ever reports a refusal: nothing is accepted, and `settle`
    passes through untouched."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def get_supported(self) -> Any:
        return self._inner.get_supported()

    async def verify(self, payload: Any, requirements: Any) -> Any:
        from x402.schemas import VerifyResponse

        try:
            return await self._inner.verify(payload, requirements)
        except Exception as error:  # noqa: BLE001 - every error answer is a refusal
            m = _FACILITATOR_ERROR.match(str(error))
            status, body = (int(m.group(1)), m.group(2)) if m else (0, "")
            try:
                data = json.loads(body) if body else {}
            except ValueError:
                data = {}
            data = data if isinstance(data, dict) else {}
            # An invalid payment: {invalidReason, invalidMessage}; a request the API refused:
            # {errorType, errorMessage} (both with a correlationId).
            reason = (data.get("invalidReason") or data.get("errorType")
                      if 400 <= status < 500 else None)  # fmt: skip
            reason = re.sub(r"[^a-z0-9_]", "_", str(reason or "facilitator_unavailable").lower())
            note = {"reason": reason[:64], "status": str(status or "none")}
            message = re.sub(r"\s+", " ", str(data.get("invalidMessage") or data.get("errorMessage")
                                               or ""))[:200]  # fmt: skip
            if message:
                note["message"] = message
            cid = str(data.get("correlationId") or "")
            if _CORRELATION.match(cid):
                note["correlation_id"] = cid
            _facilitator_note.set(note)
            payer = data.get("payer")
            return VerifyResponse(is_valid=False, invalid_reason=reason[:64],
                                  invalid_message=message or None,
                                  payer=payer if isinstance(payer, str) else None)  # fmt: skip


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


def _signed_by_payer(payload: Any, settings: Settings, units: int = 1) -> str | None:
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
        if (payload.payload or {}).get("transaction"):
            return _svm_signed_by_payer(payload, settings, units)
        evm = ExactEIP3009Payload.from_dict(payload.payload)
        auth = evm.authorization
        price_units = int(Decimal(settings.price_usd) * 10**6) * units
        ours = settings.pay_to_for(req.network)
        if (
            ours is None
            or EXTRA_NETWORKS.get(req.network, "evm") != "evm"
            or req.pay_to.lower() != ours.lower()
            or auth.to.lower() != ours.lower()
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


def _svm_signed_by_payer(payload: Any, settings: Settings, units: int = 1) -> str | None:
    """The payer, if this is a Solana USDC transfer to our address on that network at our price,
    signed by its token authority. Checked locally (signature over the message; nothing is
    broadcast), so a retry can be answered without a second charge, as on EVM."""
    from solders.message import to_bytes_versioned
    from solders.pubkey import Pubkey
    from x402.mechanisms.svm.types import ExactSvmPayload
    from x402.mechanisms.svm.utils import (
        decode_transaction_from_payload,
        derive_ata,
        extract_transaction_info,
        get_token_payer_from_transaction,
        get_usdc_address,
    )

    try:
        req = payload.accepted
        ours = settings.pay_to_for(req.network)
        if ours is None or EXTRA_NETWORKS.get(req.network) != "svm" or req.pay_to != ours:
            return None
        tx = decode_transaction_from_payload(ExactSvmPayload.from_dict(payload.payload))
        info = extract_transaction_info(tx)
        price_units = int(Decimal(settings.price_usd) * 10**6) * units
        if (
            info is None
            or info.mint != get_usdc_address(req.network)
            or int(info.amount) != price_units
            or info.destination_ata != derive_ata(ours, info.mint, info.token_program)
        ):
            return None
        payer = get_token_payer_from_transaction(tx)
        keys = list(tx.message.account_keys)
        index = keys.index(Pubkey.from_string(payer))
        # The signed bytes are the versioned serialization (a v0 message carries a 0x80 prefix).
        if tx.signatures[index].verify(keys[index], to_bytes_versioned(tx.message)):
            return payer
    except Exception:  # malformed or unsupported: fall through to the paid path
        return None
    return None


_BAZAAR_STATUSES = frozenset({"success", "processing", "rejected"})


def _http_resource(settings: Settings, task: Any) -> Any:
    from x402.schemas import ResourceInfo

    return ResourceInfo(
        url=settings.resource_url, description=task.description, mime_type="application/json",
        service_name=task.listing_name or task.service_name, tags=list(task.tags)[:5],
        icon_url=settings.icon_url,
    )  # fmt: skip


def _discovery_on_settle(resource: Any, count: Any) -> Any:
    """A before-settle hook: the settle call carries our discovery metadata.

    The CDP facilitator catalogues a resource in its Bazaar only when the settlement call
    carries `paymentPayload.extensions.bazaar` and `paymentPayload.resource`. Standard clients
    echo both from the 402, but some don't, and the facilitator then reports `not_submitted`
    (seen in production). This fills in what the buyer left out, from the route's own
    declaration, and never replaces what the buyer sent. Neither field is part of the signed
    authorization, so the payment itself is untouched. Counted as `bazaar_supplied:<what>`."""

    def hook(context: Any) -> None:
        try:
            payload = context.payment_payload
            if getattr(payload, "x402_version", None) != 2:
                return
            supplied = []
            declared = (context.declared_extensions or {}).get("bazaar")
            current = dict(payload.extensions or {})
            if declared and not current.get("bazaar"):
                payload.extensions = {**current, "bazaar": declared}
                supplied.append("extension")
            if getattr(payload, "resource", None) is None and resource is not None:
                payload.resource = resource
                supplied.append("resource")
            count("bazaar_supplied", "+".join(supplied) or "none")
        except Exception:  # noqa: BLE001 - discovery metadata never affects a settlement
            return

    return hook


def _bazaar_status(extension_responses: Any) -> tuple[str, str | None]:
    """(status, reason) of the facilitator's `bazaar` extension response for a settlement.

    status is one of success | processing | rejected (as the facilitator reports it),
    `not_submitted` (the response carries no `bazaar` key: discovery wasn't submitted with this
    payment), `absent` (no extension responses at all) or `unknown` (unexpected shape). The
    reason is the facilitator's own rejection text, shortened; it never contains buyer data.
    """
    if not isinstance(extension_responses, dict):
        return "absent", None
    entry = extension_responses.get("bazaar")
    if entry is None:
        return "not_submitted", None
    if not isinstance(entry, dict):
        return "unknown", None
    status = str(entry.get("status") or "").lower()
    reason = entry.get("rejectedReason") or entry.get("reason")
    reason = str(reason)[:200] if reason else None
    return (status if status in _BAZAAR_STATUSES else "unknown"), reason


def _payment_ref(payload: Any) -> tuple[str, str] | None:
    """(ledger key, payer) of a signed x402 payment, or None.

    EVM (EIP-3009): `payer:nonce`, lowercased, as before. Solana: `payer:svm:<message hash>`;
    the payer is the token-transfer authority read from the transaction with the SDK's own
    helper, and base58 case is kept. The message hash is what the facilitator itself dedupes on,
    so the same signed transfer always maps to the same ledger row."""
    inner = getattr(payload, "payload", None) or {}
    if isinstance(inner, dict) and inner.get("transaction"):
        return _svm_ref(inner)
    auth = inner.get("authorization") if isinstance(inner, dict) else None
    if not isinstance(auth, dict) or not auth.get("nonce") or not auth.get("from"):
        return None
    payer = str(auth["from"]).lower()
    return f"{payer}:{str(auth['nonce']).lower()}", payer


def _svm_ref(inner: dict[str, Any]) -> tuple[str, str] | None:
    try:
        from x402.mechanisms.svm.types import ExactSvmPayload
        from x402.mechanisms.svm.utils import (
            decode_transaction_from_payload,
            get_token_payer_from_transaction,
            transaction_message_hash,
        )

        tx = decode_transaction_from_payload(ExactSvmPayload.from_dict(inner))
        payer = get_token_payer_from_transaction(tx)
        if not payer:
            return None
        return f"{payer}:svm:{transaction_message_hash(tx)}", payer
    except Exception:  # malformed: not a payment we can key
        return None


def _paid_network(payload: Any, default: str) -> str:
    accepted = getattr(payload, "accepted", None)
    return str(getattr(accepted, "network", None) or default)


def _mcp_url(settings: Settings) -> str:
    """Where the paid MCP tool is mounted: next to the page route, at <prefix>/mcp/."""
    return settings.resource_url.rsplit("/v1/", 1)[0] + "/mcp/"


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
        f"{settings.network} to {settings.x402_recipient}). Sign it and repeat the request with "
        "the PAYMENT-SIGNATURE header. HTTP 200 = delivered and paid; any error = not charged.\n"
        "A delivered page has title, content_markdown, word_count, content_sha256 and metadata: "
        "description, canonical_url, authors, published_at, modified_at (ISO 8601 as the page "
        "wrote it), language, site_name, image and type, each exactly as the page declares it "
        "and null (authors: []) when it doesn't.\n"
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
        + (
            f"Also an MCP tool: {_mcp_url(settings)} (MCP streamable HTTP) has the tool "
            "read_page(url, request_id?). Call it unpaid to get the x402 PaymentRequired (same "
            'price, network and address); pay by repeating the call with _meta["x402/payment"] '
            "(x402 MCP transport). Settled only after the page is delivered.\n"
            if settings.mcp
            else ""
        )
        + "Errors (never charged): 422 {error: unsupported_url | non_public_address | "
        "dns_failed | disallowed_by_robots_txt | unsupported_content_type | too_large | "
        "too_many_redirects | no_readable_content (no text without running JavaScript) | "
        "bot_check (the site served a bot check instead of the page) | "
        "upstream_status (the site answered 4xx: missing, gone or "
        "forbidden; detail is its status)} means retrying the same URL won't help; "
        "502 {error: upstream_status (the site answered 5xx, 408 or 429) | fetch_failed} "
        "and 504 {error: timeout} may succeed later.\n"
    )


# A bot-check page is refused only when it is also short: the marker alone could, in theory, sit on
# a real page that carries a challenge widget next to its content.
BOT_CHECK_MAX_WORDS = 150


async def read_page(
    reader: Any, url: str, event: Callable[[str, str], None]
) -> tuple[int, dict[str, Any]]:
    """Fetch and convert one page: (200, row) or (422/502/504, error). Payment-agnostic; shared
    by the single-page task and the batch."""
    try:
        got = await reader.fetch(url.strip())
    except fetch.Refused as exc:
        event("refused", exc.code)
        status = refusal_status(exc)
        return status, {"error": exc.code, "detail": exc.detail, "charged": False}
    if got.content_type == fetch.PDF_TYPE:
        return await _read_pdf(url, got, event)
    text = got.body.decode(got.encoding, "replace")
    html = got.content_type in ("text/html", "application/xhtml+xml")
    if html:
        title, content = extract.html_to_markdown(text, got.final_url)
    else:
        title, content = "", text.strip()
    vendor = meta.bot_challenge(text) if html else None
    if vendor and len(content.split()) <= BOT_CHECK_MAX_WORDS:
        # The site served its bot check instead of the page: nothing the buyer asked for.
        event("refused", "bot_check")
        return 422, {"error": "bot_check",
                     "detail": f"the site served a {vendor} bot check instead of the page",
                     "charged": False}  # fmt: skip
    if not content.strip():
        # Nothing readable without running JavaScript (or an empty body): charging for an
        # empty page would bill the buyer for nothing, so it is refused like any other.
        event("refused", "no_readable_content")
        return 422, {"error": "no_readable_content",
                     "detail": "the page has no text without running JavaScript",
                     "charged": False}  # fmt: skip
    row = {
        "url": url,
        "final_url": got.final_url,
        "title": title,
        "content_markdown": content,
        "word_count": len(content.split()),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "metadata": meta.page_metadata(text, got.final_url) if html else meta.empty_metadata(),
        "bytes": len(got.body),
    }
    return 200, row


async def _read_pdf(
    url: str, got: Any, event: Callable[[str, str], None]
) -> tuple[int, dict[str, Any]]:
    """A PDF's text layer as the page's content, its document info as the metadata."""
    pdf = _sibling("pdf")
    try:
        title, content, fields = await pdf.parse_async(got.body)
    except pdf.Unreadable as exc:
        event("refused", "unreadable_pdf")
        return 422, {"error": "unreadable_pdf", "detail": f"the PDF can't be read ({exc})",
                     "charged": False}  # fmt: skip
    if not content.strip():
        event("refused", "no_readable_content")
        return 422, {"error": "no_readable_content",
                     "detail": "the PDF has no text layer (a scan); nothing to read",
                     "charged": False}  # fmt: skip
    metadata = {**meta.empty_metadata(), **fields, "canonical_url": got.final_url}
    return 200, {
        "url": url,
        "final_url": got.final_url,
        "title": title,
        "content_markdown": content,
        "word_count": len(content.split()),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "metadata": metadata,
        "bytes": len(got.body),
    }


def page_task(reader: Any) -> Task:
    """Web Read's task: fetch one public page and return it as markdown."""

    async def deliver(body: Any, event: Callable[[str, str], None]) -> tuple[int, dict[str, Any]]:
        """Fetch and convert the page: (200, row) or (422/502/504, error). Payment-agnostic."""
        return await read_page(reader, body.url, event)

    def example(price_usd: str) -> dict[str, Any]:
        return {
            "url": "https://example.com/",
            "final_url": "https://example.com/",
            "title": "Example Domain",
            "content_markdown": "# Example Domain\n\nThis domain is for use in examples...",
            "word_count": 28,
            "content_sha256": "3f1c…",
            "fetched_at": "2026-10-16T12:00:00Z",
            "metadata": {
                "description": None,
                "canonical_url": None,
                "authors": [],
                "published_at": None,
                "modified_at": None,
                "language": "en",
                "site_name": None,
                "image": None,
                "type": None,
            },
            "receipt": {"payment_ref": "0xpayer:0xnonce", "price_usd": price_usd},
        }

    discovery = {**OUTPUT_SCHEMA, "properties": {
        k: v for k, v in OUTPUT_SCHEMA["properties"].items() if k != "metadata"}}  # fmt: skip
    return Task(
        discovery_schema=discovery,
        path="/v1/page",
        request_model=PageRequest,
        service_name="Inferrail Web Read",
        description=DESCRIPTION,
        mpp_description=MPP_DESCRIPTION,
        # Phrases agents search the Bazaar with (each at most 32 characters, at most 5).
        # Two task phrases for semantic search, plus the exact single-word tags page readers use
        # most in the Bazaar: Wallet MCP's `search_x402_services` tag filter is exact match.
        tags=["markdown", "web-scraping", "web", "web page to markdown", "url to markdown"],
        listing_name="Web Page to Markdown (Inferrail)",
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
    aws_verifier: Any = None,  # tests: a payment-token verifier for the AWS rail (aws_rail.build)
    credits: Any = None,  # prepaid workspace credits with an API key (credits_rail.Credits)
) -> FastAPI:
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
    from x402.http import HTTPFacilitatorClient
    from x402.http.middleware.fastapi import payment_middleware
    from x402.http.types import HTTPResponseBody, PaymentOption, RouteConfig
    from x402.http.utils import decode_payment_signature_header
    from x402.mechanisms.evm.exact import register_exact_evm_server
    from x402.server import x402ResourceServer

    ledger = Ledger(settings.data_dir / "web_read.sqlite3")
    reader = fetcher or fetch.Fetcher(settings.user_agent, pdf=settings.pdf)
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
    # Units this request buys (see Task.units): set per request before any offer or check.
    units_var: contextvars.ContextVar[int] = contextvars.ContextVar("inferrail_units", default=1)

    def units_of(body: Any) -> int:
        return task.units(body) if task.units else 1

    def rail_units(units: int | None = None) -> dict[str, int]:
        """The rail's `units` argument, passed only for a task priced per unit."""
        return {"units": units_var.get() if units is None else units} if task.units else {}

    mpp_network = f"tempo:{getattr(settings.mpp, 'chain_id', '')}".rstrip(":")

    def rec(type_: str, rail_name: str, **fields: Any) -> None:
        events.record(
            type_, product=product, route=route, rail=rail_name,
            pay_to=(settings.x402_recipient if rail_name == "x402" else settings.pay_to).lower(),
            **fields,
        )  # fmt: skip

    def purchase_of(ref: str | None) -> str | None:
        """The purchase a ledger payment_ref belongs to (MPP refs map to their credential)."""
        if not ref:
            return None
        known = getattr(events, "purchase_for", lambda _: None)(ref)
        return known or commerce_events.purchase_id("ledger", ref)

    def _signed_network(request: Request) -> str | None:
        header = request.headers.get("payment-signature")
        try:
            return _paid_network(decode_payment_signature_header(header), "") if header else None
        except Exception:
            return None

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
                amount = int(
                    getattr(context.result, "amount", None) or price_atomic * units_var.get()
                )
            except (TypeError, ValueError):
                amount = price_atomic * units_var.get()
            # What the facilitator said about cataloguing this resource in its discovery index
            # (CDP Bazaar), from its EXTENSION-RESPONSES header. Observability only.
            status, reason = _bazaar_status(getattr(context.result, "extension_responses", None))
            try:
                ledger.event("bazaar_index", status)
            except Exception:  # noqa: BLE001 - never in the way of a settled sale
                pass
            rec(
                "settled", "x402", purchase_id=commerce_events.purchase_id("ledger", ref[0]),
                ledger_key=ref[0], payer=ref[1], amount_atomic=amount, asset="USDC",
                tx_hash=getattr(context.result, "transaction", None) or None,
                network=getattr(context.result, "network", None)
                or _paid_network(context.payment_payload, settings.network),
                outcome="settled",
                detail={"bazaar": {"status": status, "reason": reason}},
            )  # fmt: skip

    def make_x402_server(fac: Any, resource: Any = None) -> Any:
        """A resource server for every accepted network, with Web Read's settle accounting.

        `resource` (x402 ResourceInfo) is what the settle call names when the buyer's payment
        doesn't; by default the HTTP route's."""
        built = x402ResourceServer(fac)
        evm_networks = [n for n, _ in settings.accepted() if EXTRA_NETWORKS.get(n, "evm") == "evm"]
        register_exact_evm_server(built, networks=evm_networks)
        svm_networks = [n for n, _ in settings.accepted() if EXTRA_NETWORKS.get(n) == "svm"]
        if svm_networks:
            from x402.mechanisms.svm.exact import register_exact_svm_server

            register_exact_svm_server(built, networks=svm_networks)
        built.on_before_settle(
            _discovery_on_settle(resource or _http_resource(settings, task), ledger.event)
        )
        built.on_after_settle(after_settle)
        return built

    if settings.x402:
        server = make_x402_server(ExplainedFacilitator(facilitator))
    example = task.output_example(settings.price_usd)
    offer_schema, offer_example = discovery_output(task, example)

    def unit_price(_context: Any) -> str:
        """The x402 price of this request: units x price (units set by the middleware)."""
        return f"${price_for(settings.price_usd, units_var.get())}"

    def offer(method: str) -> Any:
        body = {"body_type": "json"} if method == "POST" else {}  # GET takes the query string
        price: Any = unit_price if task.units else f"${settings.price_usd}"
        return RouteConfig(
            accepts=[
                PaymentOption(scheme="exact", pay_to=pay_to, price=price, network=net)
                for net, pay_to in settings.accepted()
            ],
            resource=settings.resource_url,
            description=task.description,
            mime_type="application/json",
            service_name=task.listing_name or task.service_name,
            tags=task.tags,
            icon_url=settings.icon_url,
            extensions=declare_discovery_extension(
                input=task.input_example,
                input_schema=task.request_model.model_json_schema(),
                output=OutputConfig(example=offer_example, schema=offer_schema),
                **body,
            ),
            unpaid_response_body=lambda _ctx: HTTPResponseBody(
                content_type="application/json",
                body=unpaid_guide(settings, task, units_var.get() if task.units else 1,
                                  credits=credits is not None),
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
    app.state.task = task  # what the route sells (the pre-deploy offer check asks for its 402)
    app.state.mcp = None
    if settings.mcp and settings.x402:
        from mcp.server.transport_security import TransportSecuritySettings

        mcp_server = _sibling("mcp_tool").build(
            settings=settings, facilitator=facilitator, make_server=make_x402_server,
            task=task, ledger=ledger, rec=rec,
            payment_ref=_payment_ref, paid_network=_paid_network,
            commerce_events=commerce_events,
        )  # fmt: skip
        host = urlsplit(settings.resource_url).netloc
        app.mount(
            "/mcp",
            mcp_server.streamable_http_app(
                streamable_http_path="/",
                stateless_http=True,
                json_response=True,
                transport_security=TransportSecuritySettings(
                    enable_dns_rebinding_protection=True,
                    allowed_hosts=[host],
                    allowed_origins=[f"https://{host}", f"http://{host}"],
                ),
            ),  # fmt: skip
        )
        app.state.mcp = mcp_server
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
            payer = _signed_by_payer(
                decode_payment_signature_header(header), settings, units_of(body)
            )
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
        headers = (
            await rail.challenge_headers(str(request.url), request.method, **rail_units())
            if rail
            else []
        )
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
        row: dict[str, Any],
        receipt: Any,
        payer: str,
        idem: str,
        client: str,
        pid: str,
        units: int = 1,
    ) -> Any:
        """Record a settled sale and answer with it. The buyer has paid: a ledger failure is
        recorded as an event and never withholds the result."""
        ref = f"tempo:{receipt.reference}"
        with contextlib.suppress(Exception):  # telemetry never withholds a paid result
            who = str(receipt.payer or payer).lower()
            fields = {"purchase_id": pid, "ledger_key": ref, "payer": who, "network": mpp_network,
                      "amount_atomic": price_atomic * units, "client": client}  # fmt: skip
            rec("settled", "mpp", tx_hash=receipt.reference, outcome="settled", **fields)
            rec("receipt_issued", "mpp", receipt_ref=ref, **fields)
        try:
            ledger.delivered(
                ref, (receipt.payer or payer).lower(), task.ledger_fields(row),
                price_for(settings.price_usd, units), idem,
                json.dumps(row) if settings.recovery_s else None,
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
        units = units_of(body)
        authorization = request.headers["authorization"]
        valid = await rail.validate(authorization, str(request.url), **rail_units(units))
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
        rec("payment_attempt", "mpp", amount_atomic=price_atomic * units, **mine)
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
                            await rail.settle(credential, **rail_units(units))
                        ledger.event("refund_owed", client)
                        rec("refund_owed", "mpp", **mine)
                        row.update(charged=True, refund="owed")
                    else:  # nothing was broadcast: the buyer may use this credential again
                        ledger.release_claim(key)
                    return JSONResponse(row, status_code=status)
                row["receipt"] = {
                    "payment_ref": None,  # set once the transfer settles
                    "price_usd": price_for(settings.price_usd, units),
                    "request_id": body.request_id,
                    "rail": "mpp-tempo",
                }
                ledger.set_claim(key, "delivered", json.dumps(row))  # before the broadcast
                if source := _source(request):
                    ledger.event("channel", f"{source}:delivered")
                rec("delivered", "mpp", **mine)
            try:
                receipt = await rail.settle(credential, **rail_units(units))
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
            return paid(row, receipt, payer, idem, client, pid, units)

    @app.middleware("http")
    async def _x402(request: Request, call_next: Any) -> Any:
        aws_path = f"{settings.mount_prefix}/aws/"
        if settings.aws is not None and request.url.path.startswith(aws_path):
            return await call_next(request)  # the AWS rail's own sub-app prices and sells these
        if credits is not None and request.url.path.startswith(f"{settings.mount_prefix}/credits/"):
            return await call_next(request)  # paid from workspace credits by the credits sub-app
        response = await sell_or_challenge(request, call_next)
        if is_page(request):  # paid pages and one-time challenges must never be served from a cache
            response.headers["cache-control"] = "no-store"
        return response

    async def sell_or_challenge(request: Request, call_next: Any) -> Any:
        client = _client_class(request.headers.get("user-agent"))
        signed = bool(request.headers.get("payment-signature"))
        page = is_page(request)
        if page and task.units:
            # A per-unit price is quoted for a valid request only: an invalid one is refused
            # here, before any offer or payment check, and costs nothing. The exception is a
            # bare, unpaid POST with no body at all (a price check, as the CDP Bazaar's
            # validator and health probe send): it gets the one-unit offer, like the single-page
            # route. Anything with a body or a payment is validated as before.
            bare_probe = (request.method == "POST" and not signed
                          and not request.headers.get("authorization")
                          and not (await request.body()).strip())  # fmt: skip
            try:
                units_var.set(1 if bare_probe else units_of(await page_request(request)))
            except Exception:
                ledger.event("refused", "invalid_request")
                return JSONResponse({"error": "invalid_request", "charged": False},
                                    status_code=422)  # fmt: skip
        source = _source(request) if page else None
        if source:
            mpp_paid = mpp_rail.is_mpp_authorization(request.headers.get("authorization"))
            paying_now = signed or mpp_paid
            ledger.event("channel", f"{source}:{'attempt' if paying_now else 'challenge'}")
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
            x402_fields["network"] = _signed_network(request) or settings.network
            x402_fields.update(
                purchase_id=commerce_events.purchase_id("ledger", paying[0]),
                ledger_key=paying[0], payer=paying[1],
            )  # fmt: skip
        if request.method == "POST" or page:
            ledger.expire(settings.recovery_s)
            if signed and page:
                rec("payment_attempt", "x402", amount_atomic=price_atomic * units_var.get(),
                    asset="USDC", **x402_fields)  # fmt: skip
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
                note = _facilitator_note.get() if not delivered else None
                rec("settlement_failed" if delivered else "payment_rejected", "x402",
                    code=_x402_rejection(response), **x402_fields,
                    **({"detail": {"facilitator": note}} if note else {}))  # fmt: skip
            else:  # the payment was verified, but nothing was delivered: not charged
                rec("delivery_failed", "x402", code=_reason("x402", f"http_{response.status_code}"),
                    **x402_fields)  # fmt: skip
        if response.status_code == 402:
            if not signed:
                events.count_challenge(product, client)
            ledger.event("payment_rejected" if signed else "challenge", client)
            if rail and page and not signed:  # offer MPP (Tempo) next to x402
                for k, v in await rail.challenge_headers(
                    str(request.url), request.method, **rail_units()
                ):
                    response.headers.append(k, v)
        return response

    if settings.aws is not None:
        try:  # fail-isolated: an AWS rail that can't start leaves the USDC and MPP rails serving
            aws_app = _sibling("aws_rail").build(
                task=task, aws=settings.aws, price_usd=settings.price_usd,
                data_dir=settings.data_dir, mount_prefix=settings.mount_prefix,
                allow_get=settings.allow_get, event=ledger.event, rec=rec,
                verifier=aws_verifier,
            )  # fmt: skip
            app.mount("/aws", aws_app)
        except Exception:
            log.exception("AWS Agentic Commerce rail not mounted")

    if credits is not None:
        try:  # fail-isolated, like the AWS rail
            app.mount("/credits", _sibling("credits_rail").build(
                task=task, credits=credits, price_usd=settings.price_usd,
                data_dir=settings.data_dir, recovery_s=settings.recovery_s,
                allow_get=settings.allow_get, event=ledger.event,
            ))  # fmt: skip
        except Exception:
            log.exception("Credits rail not mounted")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "network": settings.network}

    @app.get("/llms.txt", response_class=PlainTextResponse)
    async def guide() -> str:
        text = task.guide(settings)
        text += (
            "\n## Availability and recovery boundaries\n"
            "Commercial developer preview. Payment rails described above are generated from "
            "this deployment's settings. Recover a paid result only with the same complete "
            "request, request_id and payer identity within the stated recovery window. "
            "Use a new request_id for fresh evidence. Stored results support recovery; "
            "accounting records remain. A hash identifies returned content, not its truth.\n"
        )
        if task.path == "/v1/page":
            text += (
                "Web Read limits: public HTTP(S), ports 80/443, robots.txt respected; no "
                "JavaScript execution, login, paywall or bot-check bypass. HTML/text: 2 MB; "
                "fetch timeout 15 seconds per request; at most 3 redirects. "
                + ("PDF: text layer only, 5 MB, first 50 pages, no OCR. " if settings.pdf else
                   "PDF extraction is disabled on this deployment. ")
                + "Metadata is source-declared and may be missing or wrong. Inspect content "
                "for completeness; a JavaScript shell can yield thin text.\n"
            )
        if credits is not None:
            text += (
                "\n## Prepaid API-key access\n"
                f"POST {settings.resource_url.replace('/v1/', '/credits/v1/')} with "
                "Authorization: Bearer <workspace-key> (or x-api-key) and the same JSON input. "
                f"Costs {credits.units_per_result} credits per delivered {task.result_noun}; "
                "one credit is $0.001. 401 workspace_key_required for a missing/invalid key; "
                "402 insufficient_credits before delivery. Same workspace, complete request "
                "and request_id are required for recovery. Credit result cleanup is lazy; "
                "the recovery window is not a precise content-deletion deadline.\n"
                f"Funding and workspace creation: {json.dumps(credits.top_up)}. "
                "A person completes card checkout when offered; no automatic replenishment.\n"
            )
        return text

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
        decoded = decode_payment_signature_header(header) if header else None
        ref = _payment_ref(decoded) if decoded is not None else None
        mine: dict[str, Any] = {
            "network": _paid_network(decoded, settings.network),
            "request_key": commerce_events.request_key(getattr(body, "request_id", None)),
        }
        if ref:
            mine.update(purchase_id=commerce_events.purchase_id("ledger", ref[0]),
                        ledger_key=ref[0], payer=ref[1])  # fmt: skip
        rec("payment_verified", "x402", **mine)
        rec("delivery_started", "x402", **mine)
        units = units_of(body)
        status, row = await task.deliver(body, ledger.event)
        if status != 200:
            return JSONResponse(row, status_code=status)
        request.scope["inferrail_delivered"] = True
        if source := _source(request):
            ledger.event("channel", f"{source}:delivered")
        rec("delivered", "x402", amount_atomic=price_atomic * units, asset="USDC", **mine)
        row["receipt"] = {
            "payment_ref": ref[0] if ref else None,
            "price_usd": price_for(settings.price_usd, units),
            "request_id": body.request_id,
        }
        if ref:
            ledger.delivered(
                ref[0], ref[1], task.ledger_fields(row), price_for(settings.price_usd, units),
                task.idem(body),
                json.dumps(row) if settings.recovery_s else None,
            )  # fmt: skip
        return JSONResponse(row)

    @app.get("/v1/operator/rejections", include_in_schema=False)
    async def rejections(request: Request) -> JSONResponse:
        """Read-only, operator token only: recent purchases that didn't settle, with their
        recorded reason, the facilitator's explanation, client, route, payer and whether that
        payer ever settled a purchase here. No payment payloads, signatures or request bodies
        are stored, so none can be returned."""
        import hmac

        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not settings.operator_token or not hasattr(events, "rows"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if not hmac.compare_digest(token.encode(), settings.operator_token.encode()):
            return JSONResponse({"detail": "operator token required"}, status_code=401)
        try:
            since = float(request.query_params.get("since", "0") or 0)
            limit = max(1, min(int(request.query_params.get("limit", "100")), 500))
        except ValueError:
            return JSONResponse({"error": "invalid_request"}, status_code=422)
        failed = ("payment_rejected", "settlement_failed", "delivery_failed",
                  "duplicate_credential", "settlement_unknown")  # fmt: skip
        rows = events.rows(
            "SELECT purchase_id, type, at, product, route, rail, network, payer, code, client, "
            "attempt, detail FROM events WHERE purchase_id IN (SELECT purchase_id FROM events "
            f"WHERE type IN ({','.join('?' * len(failed))}) AND at >= ?) ORDER BY seq",
            (*failed, since),
        )  # fmt: skip
        settled_sql = "SELECT DISTINCT payer FROM events WHERE type='settled' AND payer IS NOT NULL"
        paid = {str(r["payer"]).lower() for r in events.rows(settled_sql)}
        by: dict[str, dict[str, Any]] = {}
        for r in rows:
            p = by.setdefault(r["purchase_id"], {
                "purchase_id": r["purchase_id"], "first_at": r["at"], "types": [], "codes": [],
                "facilitator": [], "attempts": 0})  # fmt: skip
            for k in ("product", "route", "rail", "network", "payer", "client"):
                p[k] = p.get(k) or r[k]
            p["last_at"] = r["at"]
            p["types"].append(r["type"])
            p["attempts"] += r["type"] == "payment_attempt"
            if r["code"] and r["code"] not in p["codes"]:
                p["codes"].append(r["code"])
            try:
                note = (json.loads(r["detail"]) if r["detail"] else {}).get("facilitator")
            except (ValueError, AttributeError):
                note = None
            if note:
                p["facilitator"].append(note)
        out = []
        for p in by.values():
            if "settled" in p["types"]:
                continue
            p["payer_ever_settled"] = bool(p["payer"]) and str(p["payer"]).lower() in paid
            p["types"] = sorted(set(p["types"]))
            out.append(p)
        out.sort(key=lambda p: p["first_at"], reverse=True)
        return JSONResponse({"product": product, "route": route, "count": len(out),
                             "rejections": out[:limit]})  # fmt: skip

    @app.get("/v1/operator/stats", include_in_schema=False)
    async def stats(request: Request) -> JSONResponse:
        import hmac

        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not settings.operator_token and not settings.stats_token:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        full = bool(settings.operator_token) and hmac.compare_digest(
            token.encode(), (settings.operator_token or "").encode())  # fmt: skip
        reporting = bool(settings.stats_token) and hmac.compare_digest(
            token.encode(), (settings.stats_token or "").encode())  # fmt: skip
        if not (full or reporting):
            return JSONResponse({"detail": "operator token required"}, status_code=401)
        out = ledger.stats()
        if not full:  # the reporting credential gets counts only, never wallet addresses
            out.pop("reads_by_payer", None)
        return JSONResponse(json.loads(json.dumps(out, default=str)))

    return app
