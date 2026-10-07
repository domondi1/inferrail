"""Durable seller intent and economics. Never retry an uncertain external effect."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, payer TEXT NOT NULL, budget INTEGER, committed INTEGER NOT NULL DEFAULT 0,
 expires REAL NOT NULL, first_request TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchases (
 id INTEGER PRIMARY KEY, payer TEXT NOT NULL, job TEXT NOT NULL REFERENCES jobs(id),
 request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
 price INTEGER NOT NULL, supplier_bound INTEGER NOT NULL, fee_bound INTEGER NOT NULL,
 nonce TEXT NOT NULL, payload TEXT NOT NULL, request TEXT NOT NULL,
 tx TEXT, settlement TEXT, result TEXT, supplier_cogs INTEGER, variable_fees INTEGER,
 refunds INTEGER NOT NULL DEFAULT 0, credits INTEGER NOT NULL DEFAULT 0,
 liability INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, delivered REAL,
 UNIQUE(payer, job, request_id), UNIQUE(payer, nonce)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, purchase INTEGER, kind TEXT NOT NULL, details TEXT NOT NULL,
 created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS aliases (
 payer TEXT NOT NULL, job TEXT NOT NULL, request_id TEXT NOT NULL, purchase INTEGER NOT NULL,
 PRIMARY KEY(payer,job,request_id), FOREIGN KEY(purchase) REFERENCES purchases(id)
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
 BEGIN SELECT RAISE(ABORT,'append-only events'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
 BEGIN SELECT RAISE(ABORT,'append-only events'); END;
"""


class Refused(ValueError):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def event(conn: sqlite3.Connection, purchase: int | None, kind: str, **details: Any) -> None:
        conn.execute(
            "INSERT INTO events(purchase,kind,details,created) VALUES(?,?,?,?)",
            (purchase, kind, json.dumps(details, sort_keys=True), time.time()),
        )

    def job(self, job: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            return dict(row) if row else None

    def get(self, purchase: int) -> dict[str, Any]:
        with self.connect() as conn:
            return dict(conn.execute("SELECT * FROM purchases WHERE id=?", (purchase,)).fetchone())

    def lookup(self, payer: str, job: str, request: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM purchases WHERE payer=? AND job=? AND request_id=?",
                (payer, job, request),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """SELECT p.* FROM aliases a JOIN purchases p ON p.id=a.purchase
                    WHERE a.payer=? AND a.job=? AND a.request_id=?""",
                    (payer, job, request),
                ).fetchone()
            return dict(row) if row else None

    def cached(
        self, payer: str, job: str, request: str, fingerprint: str, ttl: int
    ) -> dict[str, Any] | None:
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT * FROM purchases WHERE payer=? AND job=? AND fingerprint=?
                AND state='DELIVERED' AND delivered>? ORDER BY delivered DESC LIMIT 1""",
                (payer, job, fingerprint, time.time() - ttl),
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT OR IGNORE INTO aliases VALUES(?,?,?,?)",
                    (payer, job, request, row["id"]),
                )
                self.event(
                    conn,
                    row["id"],
                    "CACHE_HIT",
                    spend_avoided=row["price"],
                    cogs_avoided=row["supplier_bound"],
                )
            return dict(row) if row else None

    def reserve(
        self,
        *,
        payer: str,
        job: str,
        request: str,
        fingerprint: str,
        budget: int | None,
        authenticated: bool,
        price: int,
        supplier_bound: int,
        fee_bound: int,
        nonce: str,
        payload: str,
        body: str,
        risk_ceiling: int,
        expires: float,
    ) -> tuple[dict[str, Any], bool]:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM purchases WHERE payer=? AND job=? AND request_id=?",
                (payer, job, request),
            ).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise Refused("request_id_conflict")
                if budget is not None:
                    current = conn.execute("SELECT budget FROM jobs WHERE id=?", (job,)).fetchone()[
                        0
                    ]
                    if budget != current:
                        raise Refused("immutable_job_budget")
                return dict(row), False
            alias = conn.execute(
                """SELECT p.* FROM aliases a JOIN purchases p ON p.id=a.purchase
                WHERE a.payer=? AND a.job=? AND a.request_id=?""",
                (payer, job, request),
            ).fetchone()
            if alias:
                if alias["fingerprint"] != fingerprint:
                    raise Refused("request_id_conflict")
                return dict(alias), False
            existing = conn.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if existing:
                if not authenticated or existing["payer"] != payer:
                    raise Refused("job_token_required")
                if budget is not None and budget != existing["budget"]:
                    raise Refused("immutable_job_budget")
                if existing["expires"] <= time.time():
                    raise Refused("job_expired")
            else:
                conn.execute(
                    "INSERT INTO jobs(id,payer,budget,expires,first_request) VALUES(?,?,?,?,?)",
                    (job, payer, budget, expires, request),
                )
                existing = conn.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            canonical = conn.execute(
                """SELECT * FROM purchases WHERE payer=? AND job=? AND fingerprint=?
                AND state NOT IN ('PAYMENT_REJECTED','RESOLVED') ORDER BY created DESC LIMIT 1""",
                (payer, job, fingerprint),
            ).fetchone()
            if canonical:
                if canonical["state"] != "DELIVERED":
                    raise Refused("same_query_in_progress")
                if authenticated and canonical["delivered"] > time.time() - 300:
                    conn.execute(
                        "INSERT INTO aliases VALUES(?,?,?,?)",
                        (payer, job, request, canonical["id"]),
                    )
                    self.event(
                        conn,
                        canonical["id"],
                        "CACHE_HIT",
                        spend_avoided=price,
                        cogs_avoided=supplier_bound,
                    )
                    return dict(canonical), False
            if (
                existing["budget"] is not None
                and existing["committed"] + price > existing["budget"]
            ):
                raise Refused("job_budget_exhausted")
            # Unresolved paid service could require a full refund in addition to supplier cost.
            # Reserve this conservative exposure even before the customer settles.
            risk_sql = """SELECT COALESCE(SUM(price+supplier_bound+fee_bound),0)
                FROM purchases
                WHERE state NOT IN ('PAYMENT_REJECTED','RESOLVED')
                   OR (state='RESOLVED' AND liability > 0)"""
            risk = conn.execute(risk_sql).fetchone()[0]
            if risk + price + supplier_bound + fee_bound > risk_ceiling:
                raise Refused("unresolved_risk_ceiling")
            if conn.execute(
                "SELECT 1 FROM purchases WHERE payer=? AND nonce=?", (payer, nonce)
            ).fetchone():
                raise Refused("payment_already_bound")
            cur = conn.execute(
                """INSERT INTO purchases(
                    payer,job,request_id,fingerprint,state,price,supplier_bound,fee_bound,
                    nonce,payload,request,created
                ) VALUES(?,?,?,?,'RESERVED',?,?,?,?,?,?,?)""",
                (
                    payer,
                    job,
                    request,
                    fingerprint,
                    price,
                    supplier_bound,
                    fee_bound,
                    nonce,
                    payload,
                    body,
                    time.time(),
                ),
            )
            purchase = int(cur.lastrowid or 0)
            conn.execute("UPDATE jobs SET committed=committed+? WHERE id=?", (price, job))
            self.event(conn, purchase, "RESERVED", price=price, supplier_bound=supplier_bound)
        return self.get(purchase), True

    def transition(self, purchase: int, expected: str, target: str, **updates: Any) -> bool:
        allowed = {
            "tx",
            "settlement",
            "result",
            "supplier_cogs",
            "variable_fees",
            "refunds",
            "credits",
            "liability",
            "delivered",
        }
        if not updates.keys() <= allowed:
            raise ValueError("invalid transition fields")
        with self.transaction() as conn:
            assignments = ",".join(["state=?"] + [f"{key}=?" for key in updates])
            cur = conn.execute(
                f"UPDATE purchases SET {assignments} WHERE id=? AND state=?",
                (target, *updates.values(), purchase, expected),
            )
            if not cur.rowcount:
                return False
            if target == "PAYMENT_REJECTED":
                row = conn.execute(
                    "SELECT job,price FROM purchases WHERE id=?", (purchase,)
                ).fetchone()
                conn.execute(
                    "UPDATE jobs SET committed=committed-? WHERE id=?", (row["price"], row["job"])
                )
            self.event(conn, purchase, target, **updates)
            return True

    def resolve_financials(
        self,
        purchase: int,
        *,
        supplier_cogs: int,
        variable_fees: int,
        refunds: int = 0,
        credits: int = 0,
        refund_transaction: str | None = None,
    ) -> bool:
        """Close an unresolved paid purchase after operator evidence is reconciled."""
        amounts = (supplier_cogs, variable_fees, refunds, credits)
        if any(not isinstance(value, int) or value < 0 for value in amounts):
            raise ValueError("financial amounts must be nonnegative atomic USDC integers")
        if refunds and not refund_transaction:
            raise ValueError("a refund requires its confirmed transaction hash")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM purchases WHERE id=?", (purchase,)
            ).fetchone()
            if row is None or row["state"] in ("DELIVERED", "PAYMENT_REJECTED", "RESOLVED"):
                return False
            if row["tx"] is None:
                raise ValueError("cannot resolve financials before payment settlement is confirmed")
            conn.execute(
                """UPDATE purchases SET state='RESOLVED',supplier_cogs=?,variable_fees=?,
                refunds=?,credits=?,liability=0 WHERE id=?""",
                (supplier_cogs, variable_fees, refunds, credits, purchase),
            )
            self.event(
                conn,
                purchase,
                "FINANCIALS_RECONCILED",
                supplier_cogs=supplier_cogs,
                variable_fees=variable_fees,
                refunds=refunds,
                credits=credits,
                refund_transaction=refund_transaction,
            )
            return True

    def outstanding(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM purchases
                    WHERE state NOT IN ('DELIVERED','PAYMENT_REJECTED','RESOLVED')"""
                )
            ]
