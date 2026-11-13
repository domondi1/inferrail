"""God Mode: the operator's private view of commercial activity, served by the existing process.

Read-only over the payment ledger (`web_read.sqlite3`, opened `mode=ro`) and over the commerce
event file written by the payment code (commerce_events.py). It shows each purchase's lifecycle,
reconciles the ledger, the event history and (optionally) the chain, and produces a sanitized
evidence export. It never sells, settles, refunds or edits a ledger row.

**Off unless configured.** Nothing is mounted without `GODMODE_LOGIN_SECRET` (at least 32
characters). Access: a login form posts the secret once; the service answers with a signed,
short-lived session cookie (`HttpOnly; Secure; SameSite=Strict; Path=/ops`). Failed logins are
throttled per source and in total, and every login and export is written to an access log
(sources are keyed hashes, never IP addresses). Every response is `no-store` and `noindex` with a
strict content security policy; nothing here is listed in OpenAPI, llms.txt or the API catalog.

Optional, each off unless set:
- `GODMODE_ARCHIVE_TOKEN`: a separate read-only bearer token for `GET /ops/api/archive`, so an
  off-host job can pull the event history (and pin its hash-chain head). It grants nothing else.
- `GODMODE_CHAIN_CHECK=1`: confirm settled purchases on chain through public RPC (a few calls,
  at most every 10 minutes, only for purchases not yet confirmed).
- `GODMODE_PAYER_CLASSES`: `0xwallet=independent,0xother=affiliated`: the operator's own buyer
  classification. Anything unlisted is shown as unconfirmed; a new wallet is never assumed to be
  an independent customer.
- `GODMODE_TX_HINTS`: `payer:nonce=0xtxhash,...`: transaction hashes for x402 sales recorded
  before this recorder existed. A hint is shown as unverified until the chain check confirms it.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import hmac
import html
import json
import secrets
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

COOKIE, CSRF_COOKIE = "gm_session", "gm_csrf"
PREFIX = "/ops"
MIN_SECRET = 32
FAILS_PER_SOURCE, SOURCE_WINDOW_S = 5, 900
FAILS_TOTAL, TOTAL_WINDOW_S = 50, 3600
CHAIN_INTERVAL_S, CHAIN_BATCH = 600, 5
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
AUTHORIZATION_USED = "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"
CLASSES = ("independent", "affiliated", "test", "unconfirmed")
EXPLORERS = {
    "eip155:8453": "https://basescan.org/tx/",
    "eip155:84532": "https://sepolia.basescan.org/tx/",
    "tempo:4217": "https://explore.tempo.xyz/tx/",
}
RPCS = {
    "eip155:8453": "https://base-rpc.publicnode.com",
    "eip155:84532": "https://sepolia.base.org",
    "tempo:4217": "https://rpc.tempo.xyz",
}
STATUS_LABELS = {
    "settled": "Settled",
    "delivered_unsettled": "Delivered, settlement pending",
    "settlement_failed": "Settlement failed",
    "settlement_unknown": "Settlement unknown",
    "delivery_failed": "Delivery failed (not charged)",
    "rejected": "Payment rejected",
    "duplicate": "Duplicate payment",
    "in_progress": "In progress",
}
SECURITY_HEADERS = {
    "cache-control": "no-store",
    "x-robots-tag": "noindex, nofollow, noarchive",
    "content-security-policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'; object-src 'none'",
    "x-frame-options": "DENY",
    # same-origin, not no-referrer: with no-referrer browsers send `Origin: null` on our own
    # form posts, which the origin check (rightly) refuses. Nothing is sent to other sites.
    "referrer-policy": "same-origin",
    "x-content-type-options": "nosniff",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


def _pairs(raw: str) -> dict[str, str]:
    out = {}
    for item in filter(None, (p.strip() for p in raw.split(","))):
        key, sep, value = item.rpartition("=")
        if not sep or not key.strip() or not value.strip():
            raise RuntimeError(f"malformed God Mode setting entry: {item[:20]}...")
        out[key.strip().lower()] = value.strip().lower()
    return out


@dataclass(frozen=True)
class GodModeSettings:
    login_secret: str
    archive_token: str | None = None
    chain_check: bool = False
    payer_classes: dict[str, str] = field(default_factory=dict)
    tx_hints: dict[str, str] = field(default_factory=dict)
    rpcs: dict[str, str] = field(default_factory=lambda: dict(RPCS))
    session_s: int = 12 * 3600

    @classmethod
    def from_env(cls, env: dict[str, str]) -> GodModeSettings | None:
        secret = env.get("GODMODE_LOGIN_SECRET", "")
        if not secret:
            return None
        if len(secret) < MIN_SECRET:
            raise RuntimeError(f"GODMODE_LOGIN_SECRET must be at least {MIN_SECRET} characters")
        archive = env.get("GODMODE_ARCHIVE_TOKEN") or None
        if archive is not None and (len(archive) < MIN_SECRET or archive == secret):
            raise RuntimeError("GODMODE_ARCHIVE_TOKEN must be 32+ characters and differ")
        classes = _pairs(env.get("GODMODE_PAYER_CLASSES", ""))
        if bad := sorted(set(classes.values()) - set(CLASSES)):
            raise RuntimeError(f"GODMODE_PAYER_CLASSES: unknown class {bad[0]}")
        rpcs = dict(RPCS)
        for network in RPCS:
            override = env.get(f"GODMODE_RPC_{network.replace(':', '_').upper()}")
            if override:
                rpcs[network] = override
        return cls(
            login_secret=secret,
            archive_token=archive,
            chain_check=env.get("GODMODE_CHAIN_CHECK") == "1",
            payer_classes=classes,
            tx_hints=_pairs(env.get("GODMODE_TX_HINTS", "")),
            rpcs=rpcs,
        )


def usd(atomic: int | None) -> str:
    return "—" if atomic is None else format(Decimal(atomic) / Decimal(1_000_000), "f")


def iso(ts: float | None) -> str:
    if ts is None:
        return "—"
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def short(value: str | None, keep: int = 6) -> str:
    if not value:
        return "—"
    return value if len(value) <= 2 * keep + 2 else f"{value[: keep + 2]}…{value[-keep:]}"


# -- the data model -----------------------------------------------------------------------------


class Evidence:
    """Everything God Mode shows, computed from the ledger (read-only) and the event history."""

    def __init__(self, store: Any, ledger_path: Path, read_settings: Any, gm: GodModeSettings):
        self.store, self.ledger_path, self.read, self.gm = store, ledger_path, read_settings, gm
        self.price_atomic = int(Decimal(read_settings.price_usd) * 1_000_000)
        chain = getattr(read_settings.mpp, "chain_id", None)
        self.mpp_network = f"tempo:{chain}" if chain else "tempo:4217"
        self.last_chain_run = 0.0
        self.purchase_id = sys.modules[type(store).__module__].purchase_id
        self.prepare_error: str | None = None

    def ledger_rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if not self.ledger_path.is_file():
            return []
        db = sqlite3.connect(f"file:{self.ledger_path}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute(sql, args).fetchall()]
        finally:
            db.close()

    # Historical sales: one idempotent import per settled ledger row with no live events.
    def backfill(self) -> int:
        purchase_id = self.purchase_id
        added = 0
        rows = self.ledger_rows(
            "SELECT payment_ref, payer, price_usd, created_at FROM reads WHERE settled=1"
        )
        for row in rows:
            ref = row["payment_ref"]
            pid = self.store.purchase_for(ref) or purchase_id("ledger", ref)
            mpp = ref.startswith("tempo:")
            tx = ref.removeprefix("tempo:") if mpp else self.gm.tx_hints.get(ref.lower())
            amount = int(Decimal(row["price_usd"]) * 1_000_000)
            reconstructed = ["rail", "network", "amount_atomic", "at (ledger delivery time)"]
            if tx:
                reconstructed.append("tx_hash" if mpp else "tx_hash (operator hint, unverified)")
            common = {
                "purchase_id": pid,
                "ledger_key": ref,
                "payer": (row["payer"] or "").lower() or None,
                "pay_to": self.read.pay_to.lower(),
                "rail": "mpp" if mpp else "x402",
                "network": self.mpp_network if mpp else self.read.network,
                "amount_atomic": amount,
                "asset": None if mpp else "USDC",
                "product": "Inferrail Web Read",
                "route": f"{self.read.mount_prefix}/v1/page",
            }
            detail = {
                "source": "web_read.sqlite3 reads (settled=1)",
                "reconstructed": reconstructed,
                "not_recoverable": ["earlier attempts and rejections", "request id", "client"],
            }
            for kind in ("delivered", "settled"):
                seq = self.store.append(
                    kind,
                    at=row["created_at"],
                    provenance="ledger_backfill",
                    detail=detail,
                    tx_hash=tx if kind == "settled" else None,
                    outcome=kind,
                    **common,
                )
                added += seq is not None
        return added

    def purchases(self) -> list[dict[str, Any]]:
        events = self.store.rows("SELECT * FROM events ORDER BY seq")
        chain = self.chain_status()
        by: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in events:
            if e["purchase_id"]:
                by[e["purchase_id"]].append(e)
        out = []
        for pid, evs in by.items():
            types = Counter(e["type"] for e in evs)
            settled = next((e for e in evs if e["type"] == "settled"), None)
            first = evs[0]
            pick = lambda k, evs=evs: next((e[k] for e in evs if e[k]), None)  # noqa: E731
            if settled:
                status = "settled"
            elif types["settlement_unknown"]:
                status = "settlement_unknown"
            elif types["settlement_failed"]:
                status = "settlement_failed"
            elif types["delivered"]:
                status = "delivered_unsettled"
            elif types["delivery_failed"]:
                status = "delivery_failed"
            elif types["duplicate_credential"] and not types["payment_verified"]:
                status = "duplicate"
            elif types["payment_rejected"]:
                status = "rejected"
            else:
                status = "in_progress"
            payer = pick("payer")
            tx = (settled or {}).get("tx_hash")
            network = pick("network")
            out.append(
                {
                    "purchase_id": pid,
                    "first_at": first["at"],
                    "settled_at": settled["at"] if settled else None,
                    "status": status,
                    "product": pick("product"),
                    "route": pick("route"),
                    "rail": pick("rail"),
                    "network": network,
                    "payer": payer,
                    "pay_to": pick("pay_to"),
                    "amount_atomic": (settled or {}).get("amount_atomic") or pick("amount_atomic"),
                    "tx_hash": tx,
                    "tx_verified": chain.get(pid, {}).get("outcome") == "chain_confirmed",
                    "tx_from_hint": bool(
                        settled
                        and settled["provenance"] == "ledger_backfill"
                        and tx
                        and settled["rail"] == "x402"
                    ),
                    "explorer": EXPLORERS.get(network or "", "") + tx
                    if tx and network in EXPLORERS
                    else None,  # fmt: skip
                    "ledger_key": pick("ledger_key"),
                    "attempts": types["payment_attempt"],
                    "retries": max(types["payment_attempt"] - 1, 0),
                    "recoveries": types["recovered"],
                    "delivered": bool(types["delivered"]),
                    "provenance": "ledger_backfill"
                    if all(e["provenance"] == "ledger_backfill" for e in evs)
                    else "live",
                    "classification": self.gm.payer_classes.get(
                        (payer or "").lower(), "unconfirmed"
                    )
                    if settled
                    else None,
                    "chain": chain.get(pid),
                    "codes": sorted({e["code"] for e in evs if e["code"]}),
                    "events": len(evs),
                }
            )
        return sorted(out, key=lambda p: -(p["settled_at"] or p["first_at"]))

    def chain_status(self) -> dict[str, dict[str, Any]]:
        rows = self.store.rows(
            "SELECT purchase_id, outcome, code, at, detail FROM events "
            "WHERE type='reconciliation' AND code LIKE 'chain:%' ORDER BY seq"
        )
        return {r["purchase_id"]: r for r in rows if r["purchase_id"]}

    def timeline(self, pid: str) -> list[dict[str, Any]]:
        return self.store.rows("SELECT * FROM events WHERE purchase_id=? ORDER BY seq", (pid,))

    def summary(self) -> dict[str, Any]:
        purchases = self.purchases()
        settled = [p for p in purchases if p["status"] == "settled"]
        events = self.store.rows(
            "SELECT type, COUNT(*) AS n, COALESCE(SUM(attempt > 1), 0) AS retries FROM events "
            "GROUP BY type"
        )
        n = {r["type"]: r["n"] for r in events}
        payers = Counter(p["payer"] for p in settled if p["payer"])
        classes = Counter(
            self.gm.payer_classes.get(w.lower(), "unconfirmed") for w in payers
        )  # fmt: skip
        by_product: dict[str, int] = defaultdict(int)
        by_rail: dict[str, int] = defaultdict(int)
        days: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for p in settled:
            by_product[p["product"] or "unknown"] += p["amount_atomic"] or 0
            by_rail[f"{p['rail']} · {p['network']}"] += p["amount_atomic"] or 0
            days[iso(p["settled_at"])[:10]]["settled"] += 1
        for p in purchases:
            days[iso(p["first_at"])[:10]]["purchases"] += 1
        for row in self.store.rows(
            "SELECT hour, SUM(n) AS n FROM challenges GROUP BY hour ORDER BY hour"
        ):
            days[iso(row["hour"] * 3600)[:10]]["price_checks"] += row["n"]
        ledger = self.ledger_rows(
            "SELECT COUNT(*) AS n, COALESCE(SUM(CAST(price_usd AS REAL)), 0) AS usd "
            "FROM reads WHERE settled=1"
        )
        legacy = self.ledger_rows(
            "SELECT kind, code, COUNT(*) AS n FROM events GROUP BY kind, code"
        )
        head = self.store.head()
        return {
            "generated_at": time.time(),
            "money": {
                "settled_revenue_usd": usd(sum(p["amount_atomic"] or 0 for p in settled)),
                "settled_purchases": len(settled),
                "delivered_paid_requests": sum(1 for p in settled if p["delivered"]),
                "by_product_usd": {k: usd(v) for k, v in by_product.items()},
                "by_rail_usd": {k: usd(v) for k, v in by_rail.items()},
                "fees_usd": "0 recorded (MPP network fees are paid by the buyer; x402 facilitator "
                "fees are not recorded per sale)",
            },
            "buyers": {
                "paying_wallets": len(payers),
                "independent_confirmed": classes["independent"],
                "unconfirmed": classes["unconfirmed"],
                "affiliated_or_test": classes["affiliated"] + classes["test"],
                "repeat_buyers": sum(1 for c in payers.values() if c >= 2),
            },
            "intent": {
                "payment_attempts": n.get("payment_attempt", 0),
                "retries": sum(r["retries"] for r in events),
                "rejected": n.get("payment_rejected", 0),
                "duplicate_payments": n.get("duplicate_credential", 0),
                "delivery_failures": n.get("delivery_failed", 0),
                "settlement_failures": n.get("settlement_failed", 0),
                "settlement_unknown": n.get("settlement_unknown", 0),
                "recoveries": n.get("recovered", 0),
                "refunds_owed": n.get("refund_owed", 0),
            },
            "demand": {
                "price_checks_since_recorder": sum(d["price_checks"] for d in days.values()),
                "legacy_funnel": {f"{r['kind']}:{r['code']}": r["n"] for r in legacy},
            },
            "history": [{"day": k, **v} for k, v in sorted(days.items())],
            "reconciliation": self.reconcile_local(settled, ledger[0] if ledger else None),
            "freshness": {
                "head_seq": head["seq"],
                "head_hash": head["hash"],
                "last_event_at": head["at"],
                "chain_check": "on" if self.gm.chain_check else "off",
                "last_chain_check_at": self.last_chain_run or None,
                **self.store.health(),
                "storage": self.storage(),
            },
        }

    def reconcile_local(self, settled: list[dict[str, Any]], ledger: Any) -> dict[str, Any]:
        refs = {r["payment_ref"] for r in self.ledger_rows(
            "SELECT payment_ref FROM reads WHERE settled=1")}  # fmt: skip
        event_refs = {p["ledger_key"] for p in settled}
        events_usd = sum(p["amount_atomic"] or 0 for p in settled)
        ledger_usd = int(Decimal(str(ledger["usd"])) * 1_000_000) if ledger else 0
        issues = []
        for ref in sorted(refs - event_refs):
            issues.append({"kind": "ledger_sale_without_event", "ledger_key": ref})
        for ref in sorted(event_refs - refs):
            issues.append({"kind": "event_sale_without_ledger_row", "ledger_key": ref})
        if ledger and abs(events_usd - ledger_usd) > 1:
            issues.append(
                {"kind": "revenue_mismatch", "events_usd": usd(events_usd),
                 "ledger_usd": usd(ledger_usd)}
            )  # fmt: skip
        mismatched = [p for p in settled if (p["chain"] or {}).get("outcome") == "chain_mismatch"]
        issues += [{"kind": "chain_mismatch", "purchase_id": p["purchase_id"]} for p in mismatched]
        return {
            "ledger_settled": len(refs),
            "event_settled": len(event_refs),
            "chain_confirmed": sum(1 for p in settled if p["tx_verified"]),
            "issues": issues,
            "chain_verified": self.store.verify_chain(),
        }

    def storage(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        with contextlib.suppress(OSError):
            du = shutil.disk_usage(self.store.path.parent)
            out = {"disk_total_bytes": du.total, "disk_free_bytes": du.free}
        for f in (self.store.path, self.ledger_path):
            with contextlib.suppress(OSError):
                out[f"{f.name}_bytes"] = f.stat().st_size
        return out

    async def chain_check(self, client: Any) -> int:
        """Confirm up to CHAIN_BATCH settled purchases on chain (not yet confirmed or refuted)."""
        if not self.gm.chain_check or time.time() - self.last_chain_run < CHAIN_INTERVAL_S:
            return 0
        self.last_chain_run = time.time()
        done = 0
        for p in [p for p in self.purchases() if p["status"] == "settled"]:
            if done >= CHAIN_BATCH:
                break
            if p["chain"] or not p["tx_hash"] or p["network"] not in self.gm.rpcs:
                continue
            outcome, detail = await self._verify(client, p)
            done += 1
            if outcome == "chain_unavailable":
                continue  # try again next run; nothing is recorded for a transient failure
            self.store.record(
                "reconciliation",
                provenance="reconcile",
                purchase_id=p["purchase_id"],
                ledger_key=p["ledger_key"],
                tx_hash=p["tx_hash"],
                network=p["network"],
                outcome=outcome,
                code=f"chain:{outcome}",
                dedup_key=f"chain:{p['purchase_id']}",
                detail=detail,
            )
        return done

    async def _verify(self, client: Any, p: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            resp = await client.post(
                self.gm.rpcs[p["network"]],
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_getTransactionReceipt",
                    "params": [p["tx_hash"]],
                },  # fmt: skip
                timeout=8,
            )
            receipt = resp.json().get("result")
        except Exception as exc:  # noqa: BLE001
            return "chain_unavailable", {"error": type(exc).__name__}
        if not receipt:
            return "chain_unavailable", {"error": "no_receipt"}
        pay_to = (p["pay_to"] or "").lower()
        transfers = [
            log for log in receipt.get("logs", [])
            if log.get("topics", [None])[0] == TRANSFER and len(log["topics"]) == 3
            and "0x" + log["topics"][2][-40:] == pay_to
            and int(log.get("data") or "0x0", 16) == (p["amount_atomic"] or -1)
        ]  # fmt: skip
        checks = {
            "status_success": receipt.get("status") == "0x1",
            "transfer_to_pay_to_for_amount": bool(transfers),
            "payer_matches": bool(
                transfers and "0x" + transfers[0]["topics"][1][-40:] == (p["payer"] or "")
            ),
        }
        if p["rail"] == "x402" and p["ledger_key"] and ":" in p["ledger_key"]:
            payer, nonce = p["ledger_key"].split(":", 1)
            checks["authorization_nonce_matches"] = any(
                log.get("topics", [None])[0] == AUTHORIZATION_USED
                and "0x" + log["topics"][1][-40:] == payer
                and log["topics"][2].lower() == nonce.lower()
                for log in receipt.get("logs", [])
                if len(log.get("topics", [])) == 3
            )
        detail = {"block": receipt.get("blockNumber"), "checks": checks}
        return ("chain_confirmed" if all(checks.values()) else "chain_mismatch"), detail

    def investor_export(self) -> dict[str, Any]:
        s = self.summary()
        rows = []
        for p in self.purchases():
            if p["status"] != "settled":
                continue
            rows.append(
                {
                    "settled_at_utc": iso(p["settled_at"]),
                    "product": p["product"],
                    "rail": p["rail"],
                    "network": p["network"],
                    "amount_usd": usd(p["amount_atomic"]),
                    "transaction": p["tx_hash"],
                    "explorer": p["explorer"],
                    "settlement_evidence": "confirmed on chain by God Mode"
                    if p["tx_verified"]
                    else "recorded by the payment service; chain check pending",
                    "delivery_evidence": "the service records a sale only after delivering it",
                    "buyer": {
                        "independent": "independent (operator-confirmed)",
                        "affiliated": "affiliated (not customer revenue)",
                        "test": "test (not customer revenue)",
                    }.get(p["classification"] or "", "independence not confirmed"),
                    "record": "reconstructed from the ledger"
                    if p["provenance"] == "ledger_backfill"
                    else "recorded live",
                }
            )
        return {
            "generated_at_utc": iso(time.time()),
            "what_this_shows": "Settled, delivered purchases of Inferrail services with the "
            "public transaction for each. It does not show demand, retention or product-market "
            "fit.",
            "totals": {
                "settled_purchases": s["money"]["settled_purchases"],
                "settled_revenue_usd": s["money"]["settled_revenue_usd"],
                "independent_buyers_confirmed": s["buyers"]["independent_confirmed"],
                "buyers_not_confirmed_independent": s["buyers"]["unconfirmed"],
                "repeat_buyers": s["buyers"]["repeat_buyers"],
            },
            "purchases": rows,
            "integrity": {
                "event_history_head": s["freshness"]["head_hash"],
                "chain_of_events_verified": s["reconciliation"]["chain_verified"]["ok"],
            },
        }


# -- authentication ------------------------------------------------------------------------------


class Auth:
    def __init__(self, gm: GodModeSettings, store: Any):
        self.gm, self.store = gm, store
        self.key = hashlib.sha256(b"godmode-session|" + gm.login_secret.encode()).digest()

    def _sign(self, payload: bytes) -> str:
        return hmac.new(self.key, payload, hashlib.sha256).hexdigest()

    def issue(self) -> str:
        body = json.dumps(
            {"exp": int(time.time()) + self.gm.session_s, "n": secrets.token_hex(16)}
        ).encode()
        b64 = base64.urlsafe_b64encode(body).decode().rstrip("=")
        return f"{b64}.{self._sign(b64.encode())}"

    def session(self, request: Request) -> dict[str, Any] | None:
        token = request.cookies.get(COOKIE, "")
        b64, _, sig = token.partition(".")
        if not b64 or not hmac.compare_digest(sig, self._sign(b64.encode())):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
        except ValueError:
            return None
        return data if isinstance(data, dict) and data.get("exp", 0) > time.time() else None

    def csrf_for(self, session: dict[str, Any]) -> str:
        return self._sign(f"csrf|{session['n']}".encode())

    def source(self, request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        raw = forwarded or (request.client.host if request.client else "unknown")
        return self._sign(f"source|{raw}".encode())[:16]

    def throttled(self, source: str) -> bool:
        now = time.time()
        return (
            self.store.access_failures_since(now - SOURCE_WINDOW_S, source) >= FAILS_PER_SOURCE
            or self.store.access_failures_since(now - TOTAL_WINDOW_S) >= FAILS_TOTAL
        )

    def check_secret(self, given: str) -> bool:
        return hmac.compare_digest(
            hashlib.sha256(given.encode()).digest(),
            hashlib.sha256(self.gm.login_secret.encode()).digest(),
        )


def same_origin(request: Request) -> bool:
    """The form came from this host. Compared by host, not scheme: behind the TLS-terminating
    proxy the app may see http while the browser sent https."""
    origin = request.headers.get("origin")
    if origin is None or origin == "null":
        return origin is None  # same-origin posts may omit it; the CSRF token still applies
    return urlsplit(origin).netloc == request.headers.get("host", request.url.netloc)


# -- pages ---------------------------------------------------------------------------------------


def _page(title: str, body: str, seq: int | None = None, csrf: str | None = None) -> str:
    nav = ""
    if csrf is not None:
        nav = (
            f'<nav class="nav"><a href="{PREFIX}/">Overview</a>'
            f'<a href="{PREFIX}/transactions">Transactions</a>'
            f'<a href="{PREFIX}/reconciliation">Reconciliation</a>'
            f'<a href="{PREFIX}/export">Investor export</a>'
            f'<form method="post" action="{PREFIX}/logout">'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            '<button type="submit" class="link">Sign out</button></form></nav>'
        )
    seq_attr = f' data-seq="{seq}"' if seq is not None else ""
    body = body.replace("<table>", '<div class="tw"><table>').replace("</table>", "</table></div>")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f"<title>{html.escape(title)} · God Mode</title>"
        f'<link rel="stylesheet" href="{PREFIX}/static/app.css"></head>'
        f'<body{seq_attr}><header class="top"><div class="brand">Inferrail <span>God Mode</span>'
        f"</div>{nav}</header><main>{body}</main>"
        f'<script src="{PREFIX}/static/app.js"></script></body></html>'
    )


def _e(value: Any) -> str:
    return html.escape("—" if value in (None, "") else str(value))


def _tiles(items: list[tuple[str, Any, str]]) -> str:
    return '<div class="tiles">' + "".join(
        f'<div class="tile"><div class="label">{_e(k)}</div><div class="value">{_e(v)}</div>'
        f'<div class="note">{html.escape(str(n)) if n else ""}</div></div>'
        for k, v, n in items
    ) + "</div>"  # fmt: skip


def _status(p: dict[str, Any]) -> str:
    return f'<span class="badge {p["status"]}">{_e(STATUS_LABELS[p["status"]])}</span>'


def _class_badge(c: str | None) -> str:
    if c is None:
        return "—"
    return f'<span class="badge cls-{c}">{_e(c)}</span>'


def overview_html(s: dict[str, Any], purchases: list[dict[str, Any]], csrf: str) -> str:
    m, b, i, d, f = s["money"], s["buyers"], s["intent"], s["demand"], s["freshness"]
    rec = s["reconciliation"]
    warn = (
        '<p class="callout">Settled purchases show that the payment and delivery path works. '
        "They are not, by themselves, evidence of repeatable demand or retention.</p>"
    )
    gaps = f["gaps_recorded"]
    health = []
    if f["lost_since_last_write"] or gaps["n"]:
        health.append(
            f"Telemetry gap: {f['lost_since_last_write'] + int(gaps['lost'] or 0)} event(s) not "
            "recorded because of a storage error. The payment ledger is unaffected."
        )
    if not rec["chain_verified"]["ok"]:
        health.append(
            f"Event history failed verification at seq {rec['chain_verified']['first_bad_seq']}."
        )
    health += [f"Reconciliation: {x['kind'].replace('_', ' ')}" for x in rec["issues"]]
    health_html = "".join(f'<p class="alert">{_e(h)}</p>' for h in health)
    recent = (
        "".join(_row(p) for p in purchases[:10])
        or '<tr><td colspan="7">No purchases yet.</td></tr>'
    )
    history = "".join(
        f"<tr><td>{_e(h['day'])}</td><td>{h.get('settled', 0)}</td><td>{h.get('purchases', 0)}</td>"
        f"<td>{h.get('price_checks', 0)}</td></tr>"
        for h in reversed(s["history"][-30:])
    )  # fmt: skip
    breakdown = "".join(
        f"<tr><td>{_e(k)}</td><td>${_e(v)}</td></tr>"
        for k, v in {**m["by_product_usd"], **m["by_rail_usd"]}.items()
    )
    body = (
        f"<h1>Commercial activity</h1>{warn}{health_html}"
        "<h2>Money</h2>"
        + _tiles([
            ("Settled revenue", f"${m['settled_revenue_usd']}", "sum of settled purchases"),
            ("Settled purchases", m["settled_purchases"], "each one settled on chain"),
            ("Delivered paid requests", m["delivered_paid_requests"], "settled and delivered"),
            ("Confirmed on chain", rec["chain_confirmed"], f"chain check {f['chain_check']}"),
        ])
        + "<h2>Buyers</h2>"
        + _tiles([
            ("Paying wallets", b["paying_wallets"], "a wallet is not a customer"),
            ("Independent buyers", b["independent_confirmed"], "operator-confirmed"),
            ("Independence unconfirmed", b["unconfirmed"], "not counted as customers"),
            ("Repeat buyers", b["repeat_buyers"], "2+ settled purchases"),
        ])
        + "<h2>Purchase attempts</h2>"
        + _tiles([
            ("Payment attempts", i["payment_attempts"], f"{i['retries']} retries"),
            ("Rejected", i["rejected"], f"{i['duplicate_payments']} duplicate payments"),
            ("Delivery failures", i["delivery_failures"], "not charged"),
            ("Settlement failures", i["settlement_failures"] + i["settlement_unknown"],
             f"{i['settlement_unknown']} unknown"),
            ("Recoveries", i["recoveries"], "paid result returned again, no charge"),
        ])
        + "<h2>Demand</h2>"
        + _tiles([("Price checks (402)", d["price_checks_since_recorder"],
                   "since the recorder started; not purchases")])
        + "<section><h2>Recent purchases</h2>"
        '<table><thead><tr><th>Time</th><th>Product</th><th>Rail</th><th>Amount</th>'
        f"<th>Status</th><th>Buyer</th><th>Tx</th></tr></thead><tbody>{recent}</tbody></table>"
        f'<p><a href="{PREFIX}/transactions">All transactions →</a></p></section>'
        '<div class="grid2">'
        "<section><h2>Revenue by product and rail</h2><table><tbody>"
        f"{breakdown or '<tr><td>No revenue yet.</td></tr>'}</tbody></table>"
        "</section><section><h2>Activity by day</h2><table><thead><tr><th>Day</th><th>Settled</th>"
        f"<th>Purchases</th><th>Price checks</th></tr></thead><tbody>{history}</tbody></table>"
        "</section></div>"
        "<h2>Freshness and integrity</h2>"
        + _tiles([
            ("Last event", iso(f["last_event_at"]), f"seq {f['head_seq']}"),
            ("History head", short(f["head_hash"], 8), "copy off-host to pin history"),
            ("History verified", "yes" if rec["chain_verified"]["ok"] else "NO",
             f"{rec['chain_verified']['checked']} events"),
            ("Disk free", _bytes(f["storage"].get("disk_free_bytes")),
             f"of {_bytes(f['storage'].get('disk_total_bytes'))}"),
        ])
        + "<h3>Before the recorder (aggregate counts from the ledger)</h3>"
        + '<p class="muted">These counts carry no purchase identity, so they cannot be linked to '
        "individual purchases.</p>"
        + "<table><tbody>"
        + "".join(f"<tr><td>{_e(k)}</td><td>{v}</td></tr>" for k, v in sorted(d["legacy_funnel"].items()))  # noqa: E501
        + "</tbody></table>"
    )  # fmt: skip
    return _page("Overview", body, f["head_seq"], csrf)


def _bytes(n: int | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return str(n)


def _row(p: dict[str, Any]) -> str:
    tx = (
        f'<a href="{_e(p["explorer"])}" rel="noreferrer noopener" target="_blank">'
        f"{_e(short(p['tx_hash']))}</a>"
        if p["explorer"]
        else _e(short(p["tx_hash"]))
    )
    return (
        f'<tr><td><a href="{PREFIX}/transactions/{_e(p["purchase_id"])}">{_e(iso(p["settled_at"] or p["first_at"]))}</a></td>'  # noqa: E501
        f"<td>{_e(p['product'])}</td><td>{_e(p['rail'])} · {_e(p['network'])}</td>"
        f"<td>${_e(usd(p['amount_atomic']))}</td><td>{_status(p)}</td>"
        f"<td>{_class_badge(p['classification'])}</td><td>{tx}</td></tr>"
    )  # fmt: skip


FILTERS = ("status", "rail", "network", "product", "wallet", "q", "from", "to", "reason")


def filter_purchases(purchases: list[dict[str, Any]], f: dict[str, str]) -> list[dict[str, Any]]:
    out = purchases
    if f.get("status"):
        out = [p for p in out if p["status"] == f["status"]]
    for key in ("rail", "network", "product"):
        if f.get(key):
            out = [p for p in out if (p[key] or "") == f[key]]
    if f.get("wallet"):
        out = [p for p in out if (p["payer"] or "").startswith(f["wallet"].lower())]
    if f.get("reason"):
        out = [p for p in out if any(f["reason"] in c for c in p["codes"])]
    if f.get("q"):
        q = f["q"].lower()
        out = [
            p for p in out
            if q in (p["tx_hash"] or "").lower() or q in (p["ledger_key"] or "").lower()
            or q in p["purchase_id"]
        ]  # fmt: skip
    if f.get("from"):
        out = [p for p in out if iso(p["first_at"])[:10] >= f["from"]]
    if f.get("to"):
        out = [p for p in out if iso(p["first_at"])[:10] <= f["to"]]
    return out


def transactions_html(
    purchases: list[dict[str, Any]], f: dict[str, str], seq: int, csrf: str
) -> str:
    rows = filter_purchases(purchases, f)

    def select(name: str, values: list[str], labels: dict[str, str] | None = None) -> str:
        opts = "".join(
            f'<option value="{_e(v)}"{" selected" if f.get(name) == v else ""}>'
            f"{_e((labels or {}).get(v, v))}</option>"
            for v in values
        )
        return f'<label>{name.title()}<select name="{name}"><option value="">Any</option>{opts}</select></label>'  # noqa: E501

    distinct = lambda k: sorted({p[k] for p in purchases if p[k]})  # noqa: E731
    form = (
        f'<form class="filters" method="get" action="{PREFIX}/transactions">'
        + select("status", list(STATUS_LABELS), STATUS_LABELS)
        + select("rail", distinct("rail"))
        + select("network", distinct("network"))
        + select("product", distinct("product"))
        + f'<label>Wallet<input name="wallet" value="{_e(f.get("wallet", ""))}" placeholder="0x…"></label>'  # noqa: E501
        + f'<label>Hash or id<input name="q" value="{_e(f.get("q", ""))}"></label>'
        + f'<label>Reason<input name="reason" value="{_e(f.get("reason", ""))}" placeholder="x402:…"></label>'  # noqa: E501
        + f'<label>From<input type="date" name="from" value="{_e(f.get("from", ""))}"></label>'
        + f'<label>To<input type="date" name="to" value="{_e(f.get("to", ""))}"></label>'
        + f'<button type="submit">Filter</button><a class="link" href="{PREFIX}/transactions">Clear</a>'  # noqa: E501
        + "</form>"
    )  # fmt: skip
    body = (
        f"<h1>Transactions</h1>{form}<p class=\"muted\">{len(rows)} of {len(purchases)} purchase "
        "lifecycles. A retry of the same payment stays in its purchase.</p>"
        "<table><thead><tr><th>Time</th><th>Product</th><th>Rail</th><th>Amount</th><th>Status</th>"
        "<th>Buyer</th><th>Tx</th></tr></thead><tbody>"
        + ("".join(_row(p) for p in rows) or '<tr><td colspan="7">Nothing matches.</td></tr>')
        + "</tbody></table>"
    )  # fmt: skip
    return _page("Transactions", body, seq, csrf)


def detail_html(p: dict[str, Any], timeline: list[dict[str, Any]], seq: int, csrf: str) -> str:
    chain = p["chain"] or {}
    checks = json.loads(chain["detail"]).get("checks", {}) if chain.get("detail") else {}
    tx_note = ""
    if p["tx_from_hint"] and not p["tx_verified"]:
        tx_note = " (operator hint, not yet confirmed on chain)"

    def dl(items: list[tuple[str, Any]]) -> str:
        return "<dl>" + "".join(f"<dt>{_e(k)}</dt><dd>{v}</dd>" for k, v in items) + "</dl>"

    tx = (
        f'<a href="{_e(p["explorer"])}" rel="noreferrer noopener" target="_blank">{_e(p["tx_hash"])}</a>'  # noqa: E501
        if p["explorer"] else _e(p["tx_hash"])
    )  # fmt: skip
    events = "".join(
        f'<li class="ev {e["type"]}"><div class="when">{_e(iso(e["at"]))}</div>'
        f'<div class="what"><strong>{_e(e["type"].replace("_", " "))}</strong>'
        f"{' · ' + _e(e['code']) if e['code'] else ''}"
        f"{' · attempt ' + _e(e['attempt']) if e['attempt'] else ''}"
        f'<div class="muted">source {_e(e["source"])} · {_e(e["provenance"])} · seq {e["seq"]}'
        f"{' · observed ' + _e(iso(e['observed_at'])) if e['provenance'] != 'live' else ''}</div>"
        + (f'<div class="muted">{_e(e["detail"])}</div>' if e["detail"] else "")
        + "</div></li>"
        for e in timeline
    )  # fmt: skip
    body = (
        f'<p><a href="{PREFIX}/transactions">← Transactions</a></p><h1>Purchase {_e(short(p["purchase_id"], 6))}</h1>'  # noqa: E501
        f"<p>{_status(p)} {_class_badge(p['classification'])}</p>"
        '<div class="grid2"><section><h2>Identity</h2>'
        + dl([("Purchase id", _e(p["purchase_id"])), ("Ledger key", _e(p["ledger_key"])),
              ("Product", _e(p["product"])), ("Endpoint", _e(p["route"])),
              ("Payer", _e(p["payer"])), ("Receiving wallet", _e(p["pay_to"]))])
        + "<h2>Payment</h2>"
        + dl([("Rail · network", f"{_e(p['rail'])} · {_e(p['network'])}"),
              ("Amount", f"${_e(usd(p['amount_atomic']))} ({_e(p['amount_atomic'])} atomic)"),
              ("Transaction", tx + _e(tx_note) if p["tx_hash"] else "—"),
              ("Settled at", _e(iso(p["settled_at"]))),
              ("On-chain check", _e(chain.get("outcome", "not checked"))
               + "".join(f"<br><span class=\"muted\">{_e(k)}: {_e(v)}</span>" for k, v in checks.items()))])  # noqa: E501
        + "</section><section><h2>Delivery and lifecycle</h2>"
        + dl([("Delivered", "yes" if p["delivered"] else "no"), ("Attempts", p["attempts"]),
              ("Retries", p["retries"]), ("Recoveries", p["recoveries"]),
              ("Reasons", _e(", ".join(p["codes"]) or "—")),
              ("Record", "reconstructed from the ledger (earlier attempts unknown)"
               if p["provenance"] == "ledger_backfill" else "recorded live"),
              ("Buyer classification", _e(p["classification"] or "—")
               + ('<br><span class="muted">operator setting; never inferred from a new wallet</span>'  # noqa: E501
                  if p["classification"] else ""))])
        + f"</section></div><h2>Timeline</h2><ol class=\"timeline\">{events}</ol>"
    )  # fmt: skip
    return _page("Purchase", body, seq, csrf)


def reconciliation_html(s: dict[str, Any], seq: int, csrf: str) -> str:
    r = s["reconciliation"]
    issues = "".join(f"<li>{_e(json.dumps(x))}</li>" for x in r["issues"]) or "<li>None found.</li>"
    body = (
        "<h1>Reconciliation</h1>"
        + _tiles([
            ("Ledger settled sales", r["ledger_settled"], "web_read.sqlite3"),
            ("Recorded settled purchases", r["event_settled"], "commerce events"),
            ("Confirmed on chain", r["chain_confirmed"], f"chain check {s['freshness']['chain_check']}"),  # noqa: E501
            ("Event history", "verified" if r["chain_verified"]["ok"] else "FAILED",
             f"{r['chain_verified']['checked']} events"),
        ])
        + f"<h2>Discrepancies</h2><ul>{issues}</ul>"
        "<p class=\"muted\">Corrections are added as new annotation events; recorded events are "
        "never edited or deleted.</p>"
    )  # fmt: skip
    return _page("Reconciliation", body, seq, csrf)


def export_html(x: dict[str, Any], seq: int, csrf: str) -> str:
    rows = "".join(
        f"<tr><td>{_e(r['settled_at_utc'])}</td><td>{_e(r['product'])}</td><td>{_e(r['rail'])} · "
        f"{_e(r['network'])}</td><td>${_e(r['amount_usd'])}</td><td>"
        + (f'<a href="{_e(r["explorer"])}" rel="noreferrer noopener" target="_blank">{_e(short(r["transaction"]))}</a>'  # noqa: E501
           if r["explorer"] else _e(short(r["transaction"])))
        + f"</td><td>{_e(r['settlement_evidence'])}</td><td>{_e(r['buyer'])}</td><td>{_e(r['record'])}</td></tr>"  # noqa: E501
        for r in x["purchases"]
    )  # fmt: skip
    t = x["totals"]
    body = (
        "<h1>Investor evidence</h1>"
        f'<p class="callout">{_e(x["what_this_shows"])}</p>'
        + _tiles([
            ("Settled purchases", t["settled_purchases"], ""),
            ("Settled revenue", f"${t['settled_revenue_usd']}", ""),
            ("Independent buyers", t["independent_buyers_confirmed"], "operator-confirmed"),
            ("Not confirmed independent", t["buyers_not_confirmed_independent"], ""),
            ("Repeat buyers", t["repeat_buyers"], ""),
        ])
        + "<table><thead><tr><th>Settled</th><th>Product</th><th>Rail</th><th>Amount</th>"
        "<th>Transaction</th><th>Settlement evidence</th><th>Buyer</th><th>Record</th></tr></thead>"
        f"<tbody>{rows or '<tr><td colspan=8>No settled purchases.</td></tr>'}</tbody></table>"
        f'<p class="muted">Event history head {_e(x["integrity"]["event_history_head"])}; '
        f'verified: {_e(x["integrity"]["chain_of_events_verified"])}. '
        f'<a href="{PREFIX}/export.json">Download JSON</a> (no wallet addresses, internal codes or telemetry).</p>'  # noqa: E501
    )  # fmt: skip
    return _page("Investor export", body, seq, csrf)


def login_html(token: str, message: str = "") -> str:
    body = (
        '<section class="login"><h1>Sign in</h1>'
        + (f'<p class="alert">{_e(message)}</p>' if message else "")
        + f'<form method="post" action="{PREFIX}/login"><label>Access secret'
        '<input type="password" name="secret" autocomplete="current-password" required autofocus>'
        f'</label><input type="hidden" name="csrf" value="{_e(token)}">'
        '<button type="submit">Sign in</button></form></section>'
    )
    return _page("Sign in", body)


STATIC_DIR = Path(__file__).resolve().parent / "godmode_static"


# -- the app -------------------------------------------------------------------------------------


def create_app(
    store: Any, ledger_path: Path, read_settings: Any, gm: GodModeSettings, *, http: Any = None
) -> Starlette:
    evidence = Evidence(store, ledger_path, read_settings, gm)
    auth = Auth(gm, store)
    static = {
        "app.css": ((STATIC_DIR / "app.css").read_text(), "text/css"),
        "app.js": ((STATIC_DIR / "app.js").read_text(), "text/javascript"),
    }

    def secure(response: Response) -> Response:
        for k, v in SECURITY_HEADERS.items():
            response.headers[k] = v
        return response

    def page(content: str, status: int = 200) -> Response:
        return secure(Response(content, status_code=status, media_type="text/html"))

    def require(request: Request, api: bool = False) -> dict[str, Any] | Response:
        session = auth.session(request)
        if session is None:
            if api:
                return secure(JSONResponse({"error": "authentication required"}, status_code=401))
            return secure(RedirectResponse(f"{PREFIX}/login", status_code=303))
        return session

    async def prepare() -> None:
        try:
            evidence.backfill()
            evidence.prepare_error = None
        except Exception as exc:  # noqa: BLE001 - show the page with what is recorded
            evidence.prepare_error = type(exc).__name__
        if gm.chain_check:
            import httpx

            client = http or httpx.AsyncClient()
            try:
                await evidence.chain_check(client)
            finally:
                if http is None:
                    await client.aclose()

    async def login_form(request: Request) -> Response:
        token = secrets.token_urlsafe(24)
        response = page(login_html(token))
        response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=True, samesite="strict",
                            path=PREFIX, max_age=900)  # fmt: skip
        return response

    async def login(request: Request) -> Response:
        source = auth.source(request)
        if auth.throttled(source):
            store.log_access("login", False, source)
            return page(login_html(secrets.token_urlsafe(24), "Too many attempts. Try later."), 429)
        form = await request.form()
        token = str(form.get("csrf") or "")
        cookie = request.cookies.get(CSRF_COOKIE, "")
        csrf_ok = bool(token) and hmac.compare_digest(token, cookie) and same_origin(request)
        if not csrf_ok or not auth.check_secret(str(form.get("secret") or "")):
            store.log_access("login", False, source)
            fresh = secrets.token_urlsafe(24)
            response = page(login_html(fresh, "Sign-in failed."), 401)
            response.set_cookie(CSRF_COOKIE, fresh, httponly=True, secure=True,
                                samesite="strict", path=PREFIX, max_age=900)  # fmt: skip
            return response
        store.log_access("login", True, source)
        response = secure(RedirectResponse(f"{PREFIX}/", status_code=303))
        response.set_cookie(COOKIE, auth.issue(), httponly=True, secure=True, samesite="strict",
                            path=PREFIX, max_age=gm.session_s)  # fmt: skip
        response.delete_cookie(CSRF_COOKIE, path=PREFIX)
        return response

    async def logout(request: Request) -> Response:
        session = require(request)
        if isinstance(session, Response):
            return session
        form = await request.form()
        if not same_origin(request) or not hmac.compare_digest(
            str(form.get("csrf") or ""), auth.csrf_for(session)
        ):
            return page(_page("Error", "<p>Request refused.</p>"), 403)
        store.log_access("logout", True, auth.source(request))
        response = secure(RedirectResponse(f"{PREFIX}/login", status_code=303))
        response.delete_cookie(COOKIE, path=PREFIX)
        return response

    async def overview(request: Request) -> Response:
        session = require(request)
        if isinstance(session, Response):
            return session
        await prepare()
        s = evidence.summary()
        return page(overview_html(s, evidence.purchases(), auth.csrf_for(session)))

    async def transactions(request: Request) -> Response:
        session = require(request)
        if isinstance(session, Response):
            return session
        await prepare()
        f = {k: request.query_params.get(k, "").strip()[:80] for k in FILTERS}
        seq = store.head()["seq"]
        return page(transactions_html(evidence.purchases(), f, seq, auth.csrf_for(session)))

    async def detail(request: Request) -> Response:
        session = require(request)
        if isinstance(session, Response):
            return session
        pid = request.path_params["pid"]
        found = next((p for p in evidence.purchases() if p["purchase_id"] == pid), None)
        if found is None:
            return page(
                _page("Not found", "<p>No such purchase.</p>", None, auth.csrf_for(session)), 404
            )
        seq = store.head()["seq"]
        return page(detail_html(found, evidence.timeline(pid), seq, auth.csrf_for(session)))

    async def reconciliation(request: Request) -> Response:
        session = require(request)
        if isinstance(session, Response):
            return session
        await prepare()
        return page(
            reconciliation_html(evidence.summary(), store.head()["seq"], auth.csrf_for(session))
        )

    async def export_page(request: Request) -> Response:
        session = require(request)
        if isinstance(session, Response):
            return session
        await prepare()
        store.log_access("export_view", True, auth.source(request))
        return page(
            export_html(evidence.investor_export(), store.head()["seq"], auth.csrf_for(session))
        )

    async def export_json(request: Request) -> Response:
        session = require(request, api=True)
        if isinstance(session, Response):
            return session
        await prepare()
        store.log_access("export_download", True, auth.source(request))
        response = secure(JSONResponse(evidence.investor_export()))
        response.headers["content-disposition"] = 'attachment; filename="inferrail-evidence.json"'
        return response

    async def api_summary(request: Request) -> Response:
        session = require(request, api=True)
        if isinstance(session, Response):
            return session
        head = store.head()
        return secure(JSONResponse({"head_seq": head["seq"], "last_event_at": head["at"]}))

    async def archive(request: Request) -> Response:
        if not gm.archive_token:
            return secure(JSONResponse({"detail": "Not Found"}, status_code=404))
        source = auth.source(request)
        if auth.throttled(source):
            return secure(JSONResponse({"error": "throttled"}, status_code=429))
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(
            hashlib.sha256(given.encode()).digest(),
            hashlib.sha256(gm.archive_token.encode()).digest(),
        ):
            store.log_access("login", False, source)  # counts toward the same throttle
            return secure(JSONResponse({"error": "token required"}, status_code=401))
        try:
            after = max(int(request.query_params.get("after", "0")), 0)
            limit = min(max(int(request.query_params.get("limit", "1000")), 1), 5000)
        except ValueError:
            return secure(JSONResponse({"error": "invalid parameters"}, status_code=422))
        rows = store.rows("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit))
        store.log_access("archive", True, source)
        return secure(JSONResponse({
            "head": store.head(), "events": rows,
            "next_after": rows[-1]["seq"] if rows else after,
        }))  # fmt: skip

    async def asset(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in static:
            return secure(PlainTextResponse("Not Found", status_code=404))
        content, media = static[name]
        return secure(Response(content, media_type=media))

    async def not_found(request: Request) -> Response:
        return secure(PlainTextResponse("Not Found", status_code=404))

    async def missing(request: Request, exc: Exception) -> Response:
        return await not_found(request)

    app = Starlette(
        routes=[
            Route("/", overview),
            Route("/login", login_form, methods=["GET"]),
            Route("/login", login, methods=["POST"]),
            Route("/logout", logout, methods=["POST"]),
            Route("/transactions", transactions),
            Route("/transactions/{pid:str}", detail),
            Route("/reconciliation", reconciliation),
            Route("/export", export_page),
            Route("/export.json", export_json),
            Route("/api/summary", api_summary),
            Route("/api/archive", archive),
            Route("/static/{name:str}", asset),
        ],
        exception_handlers={404: missing, 405: missing},
    )
    app.state.evidence = evidence
    return app
