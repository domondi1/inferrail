"""Durable, purchase_id-keyed idempotency store for the Work Economics seller.

SQLite, one file per deployment. Every mutating operation runs inside a
single `BEGIN IMMEDIATE` transaction so concurrent writers serialize on the
database file rather than racing an in-memory structure. Amounts are stored
as canonical strings, never float.

`purchase_id` is chosen by the buyer and is independent of any HTTP
connection or TCP retry — it is the only identity the idempotency guarantee
is keyed on, so a retried or replayed request for the same purchase_id is
never charged or executed twice.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
    purchase_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    status TEXT NOT NULL,
    quoted_amount_usd TEXT NOT NULL,
    quoted_currency TEXT NOT NULL,
    rail TEXT NOT NULL,
    payment_proof_ref TEXT,
    result_json TEXT,
    receipt_json TEXT,
    created_at REAL NOT NULL,
    payment_recorded_at REAL,
    executed_at REAL
);
"""

_COLUMNS = [
    "purchase_id", "work_id", "status", "quoted_amount_usd", "quoted_currency",
    "rail", "payment_proof_ref", "result_json", "receipt_json", "created_at",
    "payment_recorded_at", "executed_at",
]


class DurablePurchaseStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _fetch(self, conn: sqlite3.Connection, purchase_id: str) -> dict | None:
        row = conn.execute(
            "SELECT * FROM purchases WHERE purchase_id = ?", (purchase_id,)
        ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def create_quote(
        self,
        purchase_id: str,
        work_id: str,
        quoted_amount_usd: str,
        quoted_currency: str,
        rail: str,
    ) -> dict:
        """Idempotent: if purchase_id already exists, returns the existing row unchanged."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if self._fetch(conn, purchase_id) is None:
                conn.execute(
                    """INSERT INTO purchases
                       (purchase_id, work_id, status, quoted_amount_usd, quoted_currency,
                        rail, created_at)
                       VALUES (?, ?, 'QUOTED', ?, ?, ?, ?)""",
                    (purchase_id, work_id, quoted_amount_usd, quoted_currency, rail, time.time()),
                )
            conn.commit()
            result = self._fetch(conn, purchase_id)
            assert result is not None
            return result
        finally:
            conn.close()

    def record_payment(self, purchase_id: str, payment_proof_ref: str) -> tuple[dict, bool]:
        """Idempotent transition QUOTED -> PAYMENT_VERIFIED.

        `newly_charged` is True only on the transaction that actually
        performed the transition — the signal a caller uses to prove "one
        intended purchase, one charge" even under replay/retry.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            record = self._fetch(conn, purchase_id)
            if record is None:
                conn.rollback()
                raise KeyError(f"no quote for purchase_id={purchase_id}")
            newly_charged = False
            if record["status"] == "QUOTED":
                conn.execute(
                    """UPDATE purchases
                       SET status = 'PAYMENT_VERIFIED',
                           payment_proof_ref = ?,
                           payment_recorded_at = ?
                       WHERE purchase_id = ?""",
                    (payment_proof_ref, time.time(), purchase_id),
                )
                newly_charged = True
            conn.commit()
            final = self._fetch(conn, purchase_id)
            assert final is not None
            return final, newly_charged
        finally:
            conn.close()

    def execute_once(self, purchase_id: str, compute_fn) -> tuple[dict, bool]:
        """Runs compute_fn() and stores its (result_json, receipt_json) tuple
        exactly once per purchase_id. A concurrent or sequential duplicate
        call observes the stored result instead of recomputing.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            record = self._fetch(conn, purchase_id)
            if record is None:
                conn.rollback()
                raise KeyError(f"no quote for purchase_id={purchase_id}")
            if record["status"] not in ("PAYMENT_VERIFIED", "EXECUTED"):
                conn.rollback()
                raise PermissionError(f"purchase_id={purchase_id} has not completed payment")
            newly_executed = False
            if record["status"] != "EXECUTED":
                result_json, receipt_json = compute_fn()
                conn.execute(
                    """UPDATE purchases
                       SET status = 'EXECUTED', result_json = ?, receipt_json = ?, executed_at = ?
                       WHERE purchase_id = ?""",
                    (result_json, receipt_json, time.time(), purchase_id),
                )
                newly_executed = True
            conn.commit()
            final = self._fetch(conn, purchase_id)
            assert final is not None
            return final, newly_executed
        finally:
            conn.close()

    def get(self, purchase_id: str) -> dict | None:
        conn = self._connect()
        try:
            return self._fetch(conn, purchase_id)
        finally:
            conn.close()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | tuple) -> dict:
        return dict(zip(_COLUMNS, row, strict=True))
