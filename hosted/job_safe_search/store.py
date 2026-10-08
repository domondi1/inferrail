"""Durable seller intent and economics. Never retry an uncertain external effect."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS deployment_identity (
 id INTEGER PRIMARY KEY CHECK(id=1), payment_domain TEXT NOT NULL, token_digest TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS deployment_identity_no_update BEFORE UPDATE ON deployment_identity
 BEGIN SELECT RAISE(ABORT,'immutable deployment identity'); END;
CREATE TRIGGER IF NOT EXISTS deployment_identity_no_delete BEFORE DELETE ON deployment_identity
 BEGIN SELECT RAISE(ABORT,'immutable deployment identity'); END;
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
 provider TEXT NOT NULL DEFAULT 'unspecified', supplier_reference TEXT,
 refunds INTEGER NOT NULL DEFAULT 0, credits INTEGER NOT NULL DEFAULT 0,
 liability INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, delivered REAL,
 UNIQUE(payer, job, request_id), UNIQUE(payer, nonce)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, purchase INTEGER, kind TEXT NOT NULL, details TEXT NOT NULL,
 created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_observations (
 payer TEXT NOT NULL, nonce TEXT NOT NULL, payload TEXT NOT NULL, amount INTEGER NOT NULL,
 network TEXT NOT NULL, purchase INTEGER REFERENCES purchases(id), state TEXT NOT NULL,
 tx TEXT, variable_fees INTEGER, created REAL NOT NULL,
 PRIMARY KEY(payer,nonce)
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
            columns = {row[1] for row in conn.execute("PRAGMA table_info(purchases)")}
            if "provider" not in columns:
                conn.execute(
                    "ALTER TABLE purchases ADD COLUMN provider TEXT NOT NULL DEFAULT 'unspecified'"
                )
            if "supplier_reference" not in columns:
                conn.execute("ALTER TABLE purchases ADD COLUMN supplier_reference TEXT")

    def bind_deployment(self, requirements: Any, token_secret: bytes) -> None:
        """Bind durable replay/budget state to its payment domain and capability key."""

        def domain(accepted: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "network": accepted["network"],
                    "scheme": accepted["scheme"],
                    "asset": accepted["asset"].lower(),
                    "pay_to": accepted["payTo"].lower(),
                    "token_name": accepted["extra"]["name"],
                    "token_version": accepted["extra"]["version"],
                },
                sort_keys=True,
            )

        expected = domain(requirements.model_dump(by_alias=True))
        token_digest = hashlib.sha256(token_secret).hexdigest()
        with self.transaction() as conn:
            bound = conn.execute("SELECT * FROM deployment_identity WHERE id=1").fetchone()
            if bound:
                if bound["payment_domain"] != expected or bound["token_digest"] != token_digest:
                    raise ValueError("database_deployment_identity_mismatch")
                return
            # Safe upgrade: all historical payments must belong to this domain.
            for row in conn.execute("SELECT payload FROM purchases"):
                try:
                    matches = domain(json.loads(row[0])["accepted"]) == expected
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("legacy_payment_domain_evidence_missing") from exc
                if not matches:
                    raise ValueError("legacy_database_payment_domain_mismatch")
            conn.execute("INSERT INTO deployment_identity VALUES(1,?,?)", (expected, token_digest))

    @contextmanager
    def writer_lease(self) -> Iterator[None]:
        """Hold one OS process lock for the complete HTTP service lifespan."""
        try:
            import fcntl
        except ImportError as exc:
            raise RuntimeError("seller_requires_POSIX_writer_lock") from exc
        lock_path = self.path.with_name(self.path.name + ".lock")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("database_already_has_a_service_writer") from exc
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

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
            row = dict(conn.execute("SELECT * FROM purchases WHERE id=?", (purchase,)).fetchone())
            row["extra_payment_liability"] = self.extra_liability(purchase)
            extra = [
                json.loads(event[0])
                for event in conn.execute(
                    "SELECT details FROM events WHERE purchase=? AND kind='EXTRA_SETTLED_PAYMENT'",
                    (purchase,),
                )
            ]
            refunds = [
                json.loads(event[0])
                for event in conn.execute(
                    "SELECT details FROM events WHERE purchase=? AND kind='EXTRA_PAYMENT_REFUNDED'",
                    (purchase,),
                )
            ]
            row["extra_settled_revenue"] = sum(item["amount"] for item in extra)
            row["extra_refunds"] = sum(item["amount"] for item in refunds)
            row["extra_variable_fees"] = sum(item["variable_fees"] for item in refunds)
            return row

    def observe_payment(
        self, payload: str, payer: str, nonce: str, purchase: int | None
    ) -> dict[str, Any] | None:
        decoded = json.loads(payload)
        with self.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM purchases WHERE payer=? AND nonce=?", (payer, nonce)
            ).fetchone():
                return None
            inserted = conn.execute(
                "INSERT OR IGNORE INTO payment_observations "
                "VALUES(?,?,?,?,?,?,'OBSERVED',NULL,NULL,?)",
                (
                    payer,
                    nonce,
                    payload,
                    int(decoded["accepted"]["amount"]),
                    decoded["accepted"]["network"],
                    purchase,
                    time.time(),
                ),
            )
            if inserted.rowcount:
                self.event(conn, purchase, "PAYMENT_OBSERVED", payer=payer, nonce=nonce)
            row = conn.execute(
                "SELECT * FROM payment_observations WHERE payer=? AND nonce=?", (payer, nonce)
            ).fetchone()
            return dict(row)

    def observed_payments(self, *, pending: bool = True) -> list[dict[str, Any]]:
        with self.connect() as conn:
            query = "SELECT * FROM payment_observations" + (
                " WHERE state='OBSERVED'" if pending else ""
            )
            return [dict(row) for row in conn.execute(query)]

    def resolve_observation(
        self,
        payer: str,
        nonce: str,
        state: str,
        tx: str | None = None,
        variable_fees: int | None = None,
    ) -> None:
        if state not in ("SETTLED", "NOT_SETTLED") or (state == "SETTLED" and not tx):
            raise ValueError("invalid_payment_observation_evidence")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT purchase FROM payment_observations "
                "WHERE payer=? AND nonce=? AND state='OBSERVED'",
                (payer, nonce),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                "UPDATE payment_observations SET state=?,tx=?,variable_fees=? "
                "WHERE payer=? AND nonce=?",
                (state, tx, variable_fees, payer, nonce),
            )
            self.event(
                conn,
                row["purchase"],
                "PAYMENT_OBSERVATION_" + state,
                payer=payer,
                nonce=nonce,
                transaction=tx,
            )

    def reconcile_unfulfilled_refund(
        self, payer: str, nonce: str, *, refund_transaction: str, variable_fees: int, evidence: str
    ) -> None:
        """Record independently confirmed full repayment; this cannot transfer money."""
        if (
            not refund_transaction.strip()
            or not evidence.strip()
            or type(variable_fees) is not int
            or variable_fees < 0
        ):
            raise ValueError("confirmed_refund_and_total_fee_evidence_required")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM payment_observations WHERE payer=? AND nonce=?",
                (payer.lower(), nonce.lower()),
            ).fetchone()
            if row is None or row["state"] != "SETTLED" or row["purchase"] is not None:
                raise ValueError("only_unfulfilled_settled_payment_can_be_refunded")
            conn.execute(
                "UPDATE payment_observations SET state='REFUNDED',variable_fees=? "
                "WHERE payer=? AND nonce=?",
                (variable_fees, payer.lower(), nonce.lower()),
            )
            self.event(
                conn,
                None,
                "PAYMENT_OBSERVATION_REFUNDED",
                payer=payer.lower(),
                nonce=nonce.lower(),
                amount=row["amount"],
                refund_transaction=refund_transaction,
                variable_fees=variable_fees,
                evidence=evidence,
            )

    def extra_liability(self, purchase: int | None = None) -> int:
        with self.connect() as conn:
            events = conn.execute(
                "SELECT purchase,details FROM events "
                "WHERE kind IN ('EXTRA_SETTLED_PAYMENT','EXTRA_PAYMENT_PENDING')"
            )
            payments = {}
            for row in events:
                if purchase is not None and row[0] != purchase:
                    continue
                details = json.loads(row[1])
                payments[(details["payer"], details["nonce"])] = details["amount"]
            for row in conn.execute(
                "SELECT purchase,details FROM events WHERE kind='EXTRA_PAYMENT_REFUNDED'"
            ):
                if purchase is not None and row[0] != purchase:
                    continue
                details = json.loads(row[1])
                payments.pop((details["payer"], details["nonce"]), None)
            observed = conn.execute(
                "SELECT amount,purchase,state,payer,nonce FROM payment_observations "
                "WHERE state='OBSERVED' OR (state='SETTLED' AND purchase IS NULL)"
            )
            return sum(payments.values()) + sum(
                row[0]
                for row in observed
                if (purchase is None or row[1] == purchase) and (row[3], row[4]) not in payments
            )

    def pending_extra_payments(self) -> list[tuple[int, dict[str, Any]]]:
        with self.connect() as conn:
            settled = {
                (json.loads(row[0])["payer"], json.loads(row[0])["nonce"])
                for row in conn.execute(
                    "SELECT details FROM events WHERE kind='EXTRA_SETTLED_PAYMENT'"
                )
            }
            return [
                (row[0], json.loads(row[1]))
                for row in conn.execute(
                    "SELECT purchase,details FROM events WHERE kind='EXTRA_PAYMENT_PENDING'"
                )
                if (json.loads(row[1])["payer"], json.loads(row[1])["nonce"]) not in settled
            ]

    def record_extra_payment(
        self,
        purchase: int,
        payer: str,
        nonce: str,
        tx: str,
        amount: int,
        *,
        pending_payload: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            # A nonce already belongs to another ordinary purchase, or was already observed.
            if conn.execute(
                "SELECT 1 FROM purchases WHERE payer=? AND nonce=?", (payer, nonce)
            ).fetchone():
                return
            kind = "EXTRA_PAYMENT_PENDING" if pending_payload else "EXTRA_SETTLED_PAYMENT"
            previous = conn.execute("SELECT details FROM events WHERE kind=?", (kind,))
            if any(
                json.loads(row[0])["nonce"] == nonce and json.loads(row[0])["payer"] == payer
                for row in previous
            ):
                return
            self.event(
                conn,
                purchase,
                kind,
                pending_payload=pending_payload,
                payer=payer,
                nonce=nonce,
                transaction=tx,
                amount=amount,
                unresolved_liability=amount,
            )

    def reconcile_extra_refund(
        self,
        purchase: int,
        nonce: str,
        *,
        refund_transaction: str,
        variable_fees: int,
        evidence: str,
    ) -> None:
        """Record a full duplicate-payment refund AFTER independent operator confirmation.

        This only records evidence. It cannot transfer funds or authorize a refund.
        """
        if (
            not refund_transaction.strip()
            or not evidence.strip()
            or type(variable_fees) is not int
            or variable_fees < 0
        ):
            raise ValueError("confirmed full refund and resolved fees/evidence required")
        with self.transaction() as conn:
            refunded = [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT details FROM events WHERE purchase=? AND kind='EXTRA_PAYMENT_REFUNDED'",
                    (purchase,),
                )
            ]
            if any(item["nonce"] == nonce for item in refunded):
                raise ValueError("duplicate refund evidence already recorded")
            extras = [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT details FROM events WHERE purchase=? AND kind='EXTRA_SETTLED_PAYMENT'",
                    (purchase,),
                )
            ]
            extra = next((item for item in extras if item["nonce"] == nonce), None)
            if extra is None:
                raise ValueError("extra incoming payment must be finalized before reconciliation")
            self.event(
                conn,
                purchase,
                "EXTRA_PAYMENT_REFUNDED",
                payer=extra["payer"],
                nonce=nonce,
                amount=extra["amount"],
                refund_transaction=refund_transaction,
                variable_fees=variable_fees,
                evidence=evidence,
            )

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
        provider: str = "unspecified",
        cache_ttl: int = 300,
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
                if authenticated and canonical["delivered"] > time.time() - cache_ttl:
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
            risk_sql = """SELECT COALESCE(SUM(price+
                MAX(supplier_bound,COALESCE(supplier_cogs,0))+
                MAX(fee_bound,COALESCE(variable_fees,0))),0)
                FROM purchases
                WHERE state NOT IN ('DELIVERED','PAYMENT_REJECTED','RESOLVED')
                   OR supplier_cogs IS NULL OR variable_fees IS NULL
                   OR liability > 0 OR credits > 0"""
            risk = conn.execute(risk_sql).fetchone()[0]
            risk += self.extra_liability()
            if risk + price + supplier_bound + fee_bound > risk_ceiling:
                raise Refused("unresolved_risk_ceiling")
            if conn.execute(
                "SELECT 1 FROM purchases WHERE payer=? AND nonce=?", (payer, nonce)
            ).fetchone():
                raise Refused("payment_already_bound")
            if conn.execute(
                "SELECT 1 FROM payment_observations WHERE payer=? AND nonce=?", (payer, nonce)
            ).fetchone():
                raise Refused("payment_observed_requires_reconciliation")
            cur = conn.execute(
                """INSERT INTO purchases(
                    payer,job,request_id,fingerprint,state,price,supplier_bound,fee_bound,
                    nonce,payload,request,created,provider
                ) VALUES(?,?,?,?,'RESERVED',?,?,?,?,?,?,?,?)""",
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
                    provider,
                ),
            )
            purchase = int(cur.lastrowid or 0)
            conn.execute("UPDATE jobs SET committed=committed+? WHERE id=?", (price, job))
            self.event(conn, purchase, "RESERVED", price=price, supplier_bound=supplier_bound)
        return self.get(purchase), True

    def supplier_blocked(self, provider: str) -> bool:
        """An observed contract breach survives restarts and stops new paid dispatches."""
        with self.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM purchases WHERE provider=? "
                    "AND supplier_cogs>supplier_bound LIMIT 1",
                    (provider,),
                ).fetchone()
                is not None
            )

    def transition(self, purchase: int, expected: str, target: str, **updates: Any) -> bool:
        allowed = {
            "tx",
            "settlement",
            "result",
            "supplier_cogs",
            "supplier_reference",
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
        evidence: str,
    ) -> bool:
        """Close an unresolved paid purchase after operator evidence is reconciled."""
        if not evidence.strip():
            raise ValueError("reconciliation requires durable financial evidence")
        amounts = (supplier_cogs, variable_fees, refunds, credits)
        if any(not isinstance(value, int) or value < 0 for value in amounts):
            raise ValueError("financial amounts must be nonnegative atomic USDC integers")
        if refunds and not refund_transaction:
            raise ValueError("a refund requires its confirmed transaction hash")
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM purchases WHERE id=?", (purchase,)).fetchone()
            if row is None:
                return False
            if row["state"] not in (
                "SUPPLIER_INFLIGHT",
                "SUPPLIER_UNKNOWN",
                "SERVICE_FAILED",
                "DELIVERED",
                "RESOLVED",
            ):
                raise ValueError("payment finality must be confirmed before reconciliation")
            if row["tx"] is None:
                raise ValueError("cannot resolve financials before payment settlement is confirmed")
            conn.execute(
                """UPDATE purchases SET state=?,supplier_cogs=?,variable_fees=?,
                refunds=?,credits=?,liability=? WHERE id=?""",
                (
                    "DELIVERED" if row["result"] is not None else "RESOLVED",
                    supplier_cogs,
                    variable_fees,
                    refunds,
                    credits,
                    credits,
                    purchase,
                ),
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
                evidence=evidence,
            )
            return True

    def recovery_deferred(self, purchase: int, phase: str) -> None:
        """Record a recovery outage once until the purchase changes state."""
        with self.transaction() as conn:
            last = conn.execute(
                "SELECT kind,details FROM events WHERE purchase=? ORDER BY id DESC LIMIT 1",
                (purchase,),
            ).fetchone()
            if (
                last
                and last[0] == "RECOVERY_DEFERRED"
                and json.loads(last[1]).get("phase") == phase
            ):
                return
            self.event(conn, purchase, "RECOVERY_DEFERRED", phase=phase)

    def outstanding(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM purchases
                    WHERE state NOT IN ('DELIVERED','PAYMENT_REJECTED','RESOLVED')"""
                )
            ]
