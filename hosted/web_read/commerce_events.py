"""Commerce event recorder: an append-only, hash-chained history of each paid request's lifecycle.

Every observable step of a purchase (payment attempt, rejection, delivery, settlement, retry,
recovery) is written here by the payment code, at the moment it happens, so the operator can see
each sale's timeline without entering anything by hand.

**It never changes a payment.** `record()` swallows its own failures: the paid request carries on
exactly as before, and the failure is counted and written as a `telemetry_gap` event at the next
successful write, so a hole in the history is visible instead of silent. The payment ledger
(`web_read.sqlite3`) stays the authority for what was sold; this file is a separate SQLite
database, with its own connection and lock.

**What it keeps:** event type and time, a purchase id derived from the payment itself (x402
`payer:nonce`, or the MPP credential), rail, network, payer and receiving addresses, amount, asset,
transaction hash, outcome and a folded reason code, client family. **What it never keeps:** request
bodies, page URLs, search queries, headers, credentials, IP addresses or results.

**Integrity:** rows can't be updated or deleted (triggers refuse it), and each row's `hash` covers
the previous row's hash and its own canonical content. A local chain shows that history wasn't
edited *after* a head that was copied somewhere else (`head()`); on its own it can't prove an
administrator never rewrote the whole file. Off-host copies of the head close that gap.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
GENESIS = "0" * 64
log = logging.getLogger("inferrail.commerce_events")

# Lifecycle event types. Unpaid price checks (402 with no payment) are counted per hour and client
# family instead: they carry no payment identity, and they are most of the traffic.
TYPES = frozenset(
    {
        "payment_attempt",  # a request carrying a payment reached the service
        "payment_rejected",  # the payment was refused (invalid, unfunded, wrong route, off)
        "duplicate_credential",  # a payment already used, presented again
        "payment_verified",  # the payment was accepted for delivery
        "delivery_started",
        "delivery_failed",  # the service couldn't deliver; nothing was charged
        "delivered",  # the result was produced for a paid request
        "settled",  # the transfer settled on chain (the sale)
        "settlement_failed",  # delivered but the transfer did not settle
        "settlement_unknown",  # the settlement outcome couldn't be established
        "payment_canceled",  # a verified payment was released before settlement
        "receipt_issued",  # the buyer was answered with a receipt
        "recovered",  # a paid result was returned again without a second charge
        "refund_owed",
        "telemetry_gap",  # events were lost to a storage failure
        "reconciliation",  # a check against the ledger or the chain
        "annotation",  # an operator note attached to a purchase (corrections never rewrite rows)
    }
)
# One per purchase: these can't be inflated by retries or a second observation.
ONCE_PER_PURCHASE = frozenset({"delivered", "settled", "receipt_issued"})
PROVENANCE = frozenset({"live", "ledger_backfill", "reconcile", "annotation"})
FIELDS = (
    "purchase_id", "ledger_key", "product", "route", "rail", "network", "payer", "pay_to",
    "amount_atomic", "asset", "tx_hash", "outcome", "code", "client", "receipt_ref", "attempt",
    "request_key",
)  # fmt: skip

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, v INTEGER NOT NULL,
  type TEXT NOT NULL, at REAL NOT NULL, observed_at REAL NOT NULL, provenance TEXT NOT NULL,
  source TEXT NOT NULL, dedup_key TEXT UNIQUE,
  purchase_id TEXT, ledger_key TEXT, product TEXT, route TEXT, rail TEXT, network TEXT,
  payer TEXT, pay_to TEXT, amount_atomic INTEGER, asset TEXT, tx_hash TEXT, outcome TEXT,
  code TEXT, client TEXT, receipt_ref TEXT, attempt INTEGER, request_key TEXT, detail TEXT,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_purchase ON events (purchase_id, seq);
CREATE INDEX IF NOT EXISTS events_type_at ON events (type, at);
CREATE INDEX IF NOT EXISTS events_ledger_key ON events (ledger_key);
CREATE INDEX IF NOT EXISTS events_request_key ON events (request_key);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'commerce events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'commerce events are append-only'); END;
-- Unpaid price checks per hour and client family (not lifecycle events).
CREATE TABLE IF NOT EXISTS challenges (
  hour INTEGER NOT NULL, product TEXT NOT NULL, client TEXT NOT NULL, n INTEGER NOT NULL,
  PRIMARY KEY (hour, product, client)
);
-- Operator access to the private dashboard (no IPs: a keyed hash of the client address).
CREATE TABLE IF NOT EXISTS access_log (
  at REAL NOT NULL, kind TEXT NOT NULL, ok INTEGER NOT NULL, source TEXT
);
CREATE INDEX IF NOT EXISTS access_log_at ON access_log (at);
"""


def purchase_id(kind: str, key: str) -> str:
    """A stable id for one purchase: `ledger` + the ledger's payment_ref (x402 `payer:nonce`), or
    `mpp` + the credential key. The same payment always maps to the same id."""
    return hashlib.sha256(f"{kind}|{key}".encode()).hexdigest()[:32]


def request_key(request_id: str | None) -> str | None:
    """A searchable stand-in for the buyer's request_id: a hash, so the id itself is not kept
    (it is chosen by the client and could carry anything)."""
    if not request_id:
        return None
    return hashlib.sha256(f"request|{request_id}".encode()).hexdigest()[:24]


def canonical(row: dict[str, Any]) -> str:
    """The hashed content of an event: every stored column except seq and the hashes."""
    keep = {k: v for k, v in row.items() if k not in ("seq", "prev_hash", "hash")}
    return json.dumps(keep, sort_keys=True, separators=(",", ":"), default=str)


def chain_hash(prev_hash: str, row: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical(row)).encode()).hexdigest()


class EventStore:
    """The commerce event file. Thread-safe; one instance per process."""

    def __init__(self, path: Path, source: str = "web_read") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.source = source
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._db.executescript(_SCHEMA)
        self.lost = 0  # events not written since the last successful write (storage failure)
        self.last_error: str | None = None
        self.last_write_at: float | None = None

    # -- writing -------------------------------------------------------------------------------

    def append(
        self,
        type_: str,
        *,
        at: float | None = None,
        provenance: str = "live",
        dedup_key: str | None = None,
        detail: dict[str, Any] | None = None,
        **fields: Any,
    ) -> int | None:
        """Append one event; returns its seq, or None if it was a duplicate. Raises on storage
        errors (use `record` on payment paths)."""
        if type_ not in TYPES:
            raise ValueError(f"unknown event type {type_}")
        if provenance not in PROVENANCE:
            raise ValueError(f"unknown provenance {provenance}")
        unknown = set(fields) - set(FIELDS)
        if unknown:
            raise ValueError(f"unknown event fields {sorted(unknown)}")
        if dedup_key is None and type_ in ONCE_PER_PURCHASE and fields.get("purchase_id"):
            dedup_key = f"{type_}:{fields['purchase_id']}"
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if (
                    dedup_key
                    and self._db.execute(
                        "SELECT 1 FROM events WHERE dedup_key=?", (dedup_key,)
                    ).fetchone()
                ):
                    self._db.execute("ROLLBACK")
                    return None
                if type_ == "payment_attempt" and fields.get("purchase_id"):
                    prior = self._db.execute(
                        "SELECT COUNT(*) FROM events WHERE purchase_id=? AND type=?",
                        (fields["purchase_id"], "payment_attempt"),
                    ).fetchone()[0]
                    fields["attempt"] = prior + 1  # 2+ is a retry of the same payment
                last = self._db.execute(
                    "SELECT hash FROM events ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                prev = last[0] if last else GENESIS
                row: dict[str, Any] = {
                    "event_id": uuid.uuid4().hex, "v": SCHEMA_VERSION, "type": type_,
                    "at": at if at is not None else now, "observed_at": now,
                    "provenance": provenance, "source": self.source, "dedup_key": dedup_key,
                    **{k: fields.get(k) for k in FIELDS},
                    "detail": json.dumps(detail, sort_keys=True) if detail else None,
                }  # fmt: skip
                row["prev_hash"], row["hash"] = prev, chain_hash(prev, row)
                cols = ",".join(row)
                cur = self._db.execute(
                    f"INSERT INTO events ({cols}) VALUES ({','.join('?' * len(row))})",
                    tuple(row.values()),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        self.last_write_at = now
        return cur.lastrowid

    def record(self, type_: str, **kwargs: Any) -> None:
        """`append` for payment paths: never raises. A failure is counted, and the count is
        written as a `telemetry_gap` event as soon as a write succeeds again."""
        try:
            if self.lost:
                lost, self.lost = self.lost, 0
                try:
                    self.append("telemetry_gap", detail={"lost": lost, "error": self.last_error})
                except Exception:
                    self.lost += lost
                    raise
            self.append(type_, **kwargs)
        except Exception as exc:  # noqa: BLE001 - telemetry must never break a sale
            self.lost += 1
            self.last_error = type(exc).__name__
            log.warning("commerce event not recorded: %s (%s)", type_, self.last_error)

    def count_challenge(self, product: str, client: str) -> None:
        """Count one unpaid price check (never raises)."""
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO challenges VALUES (?,?,?,1) ON CONFLICT(hour, product, client) "
                    "DO UPDATE SET n = n + 1",
                    (int(time.time() // 3600), product, client),
                )
        except Exception as exc:  # noqa: BLE001
            self.lost += 1
            self.last_error = type(exc).__name__

    def log_access(self, kind: str, ok: bool, source: str | None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO access_log VALUES (?,?,?,?)", (time.time(), kind, int(ok), source)
            )

    def access_failures_since(self, since: float, source: str | None = None) -> int:
        q = "SELECT COUNT(*) FROM access_log WHERE kind='login' AND ok=0 AND at >= ?"
        args: tuple[Any, ...] = (since,)
        if source is not None:
            q, args = q + " AND source=?", (since, source)
        with self._lock:
            return int(self._db.execute(q, args).fetchone()[0])

    # -- reading -------------------------------------------------------------------------------

    def rows(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, tuple(args)).fetchall()]

    def purchase_for(self, ledger_key: str) -> str | None:
        """The purchase id already recorded for a ledger payment_ref (MPP refs are only known
        once the transfer settles, so they map back to the credential's purchase)."""
        try:
            got = self.rows(
                "SELECT purchase_id FROM events WHERE ledger_key=? AND purchase_id IS NOT NULL "
                "ORDER BY seq LIMIT 1",
                (ledger_key,),
            )
        except Exception:  # noqa: BLE001 - a lookup failure falls back to the ledger id
            return None
        return got[0]["purchase_id"] if got else None

    def head(self) -> dict[str, Any]:
        """The latest (seq, hash): copy it off-host to pin the history up to here."""
        got = self.rows("SELECT seq, hash, at FROM events ORDER BY seq DESC LIMIT 1")
        return got[0] if got else {"seq": 0, "hash": GENESIS, "at": None}

    def verify_chain(self) -> dict[str, Any]:
        """Recompute every hash. Returns ok, the number of rows checked and the first bad seq."""
        prev, n = GENESIS, 0
        with self._lock:
            cursor = self._db.execute("SELECT * FROM events ORDER BY seq")
            for r in cursor:
                row = dict(r)
                n += 1
                if row["prev_hash"] != prev or chain_hash(prev, row) != row["hash"]:
                    return {"ok": False, "checked": n, "first_bad_seq": row["seq"]}
                prev = row["hash"]
        return {"ok": True, "checked": n, "first_bad_seq": None}

    def health(self) -> dict[str, Any]:
        return {
            "lost_since_last_write": self.lost,
            "last_error": self.last_error,
            "last_write_at": self.last_write_at,
            "gaps_recorded": self.rows(
                "SELECT COUNT(*) AS n, COALESCE(SUM(json_extract(detail, '$.lost')), 0) AS lost "
                "FROM events WHERE type='telemetry_gap'"
            )[0],
        }


class NullRecorder:
    """Stands in when commerce events are off: every call is a no-op."""

    lost = 0

    def record(self, type_: str, **kwargs: Any) -> None:
        return None

    def count_challenge(self, product: str, client: str) -> None:
        return None
