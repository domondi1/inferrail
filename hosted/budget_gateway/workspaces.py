"""Workspaces, governed-run metering and the credit ledger for Inferrail Hosted.

One SQLite file holds every workspace's *billing* state. Each workspace's receipts and budgets
live in their own files (see `service.py`), exactly like `hosted/cost_gateway/tenant_store.py`.

**Value metric.** A *governed run* is a distinct work id with at least one call through the
gateway in a UTC month. The first `free_runs_per_month` runs of each month are free. Every
later new run consumes one prepaid credit. Repeat calls inside an already-counted run cost
nothing more.

**Money rules (each has a test):**

- Credits are granted only from a confirmed payment and at most once per payment reference
  (`UNIQUE(source, ref)`): a replayed webhook or settle hook can't grant twice.
- An x402 purchase is recorded as *pending* before settlement and becomes credits only when the
  settle hook confirms success. A failed settlement grants nothing. A crash in between leaves a
  visible pending row for `reconcile`, never a silent loss.
- Run admission and credit consumption happen in one `BEGIN IMMEDIATE` transaction, so
  concurrent first calls for new runs can't spend the same credit twice.
- API keys are stored only as SHA-256 hashes. Provider keys never reach this module.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

KEY_PREFIX = "irw_"

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
  workspace_id TEXT PRIMARY KEY,
  key_hash TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS governed_runs (
  workspace_id TEXT NOT NULL,
  month TEXT NOT NULL,
  work_id TEXT NOT NULL,
  first_seen INTEGER NOT NULL,
  paid INTEGER NOT NULL,
  PRIMARY KEY (workspace_id, month, work_id)
);
CREATE TABLE IF NOT EXISTS credit_grants (
  source TEXT NOT NULL,
  ref TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  runs INTEGER NOT NULL CHECK (runs > 0),
  amount_usd_cents INTEGER NOT NULL,
  granted_at INTEGER NOT NULL,
  PRIMARY KEY (source, ref)
);
CREATE TABLE IF NOT EXISTS pending_purchases (
  source TEXT NOT NULL,
  ref TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  runs INTEGER NOT NULL,
  amount_usd_cents INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending', 'settled', 'failed')),
  PRIMARY KEY (source, ref)
);
"""


def month_of(ts: float) -> str:
    return time.strftime("%Y-%m", time.gmtime(ts))


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


@dataclass(frozen=True)
class Admission:
    admitted: bool
    new_run: bool
    paid: bool
    month_runs: int
    credits_remaining: int


@dataclass(frozen=True)
class Usage:
    month: str
    month_runs: int
    free_runs_per_month: int
    free_runs_remaining: int
    credits_remaining: int


class WorkspaceLedger:
    def __init__(self, path: Path, *, free_runs_per_month: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False, timeout=30
        )
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self.free_runs_per_month = free_runs_per_month

    # -- workspaces ---------------------------------------------------------------------------

    def create_workspace(self) -> tuple[str, str]:
        """Return (workspace_id, api_key). The key is shown once and stored only as a hash."""
        workspace_id = "ws_" + secrets.token_hex(10)
        api_key = KEY_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            self._db.execute(
                "INSERT INTO workspaces VALUES (?,?,?,0)",
                (workspace_id, hash_key(api_key), int(time.time())),
            )
        return workspace_id, api_key

    def authenticate(self, api_key: str) -> str | None:
        if not api_key.startswith(KEY_PREFIX):
            return None
        row = self._db.execute(
            "SELECT workspace_id FROM workspaces WHERE key_hash=? AND disabled=0",
            (hash_key(api_key),),
        ).fetchone()
        return row[0] if row else None

    def count_workspaces(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]

    # -- metering -----------------------------------------------------------------------------

    def _credits_remaining(self, workspace_id: str) -> int:
        granted = self._db.execute(
            "SELECT COALESCE(SUM(runs),0) FROM credit_grants WHERE workspace_id=?", (workspace_id,)
        ).fetchone()[0]
        consumed = self._db.execute(
            "SELECT COUNT(*) FROM governed_runs WHERE workspace_id=? AND paid=1", (workspace_id,)
        ).fetchone()[0]
        return granted - consumed

    def admit_run(self, workspace_id: str, work_id: str, now: float | None = None) -> Admission:
        """Count `work_id` as a governed run (once per month), spending a credit past the free
        allowance. Refuses (admitted=False) when the allowance and credits are exhausted."""
        now = time.time() if now is None else now
        month = month_of(now)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                runs = self._db.execute(
                    "SELECT COUNT(*) FROM governed_runs WHERE workspace_id=? AND month=?",
                    (workspace_id, month),
                ).fetchone()[0]
                credits = self._credits_remaining(workspace_id)
                known = self._db.execute(
                    "SELECT 1 FROM governed_runs WHERE workspace_id=? AND month=? AND work_id=?",
                    (workspace_id, month, work_id),
                ).fetchone()
                if known:
                    self._db.execute("COMMIT")
                    return Admission(True, False, False, runs, credits)
                paid = runs >= self.free_runs_per_month
                if paid and credits <= 0:
                    self._db.execute("COMMIT")
                    return Admission(False, False, False, runs, credits)
                self._db.execute(
                    "INSERT INTO governed_runs VALUES (?,?,?,?,?)",
                    (workspace_id, month, work_id, int(now), int(paid)),
                )
                self._db.execute("COMMIT")
                return Admission(True, True, paid, runs + 1, credits - int(paid))
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def usage(self, workspace_id: str, now: float | None = None) -> Usage:
        month = month_of(time.time() if now is None else now)
        runs = self._db.execute(
            "SELECT COUNT(*) FROM governed_runs WHERE workspace_id=? AND month=?",
            (workspace_id, month),
        ).fetchone()[0]
        return Usage(
            month,
            runs,
            self.free_runs_per_month,
            max(self.free_runs_per_month - runs, 0),
            self._credits_remaining(workspace_id),
        )

    # -- credits ------------------------------------------------------------------------------

    def grant(
        self, source: str, ref: str, workspace_id: str, runs: int, amount_usd_cents: int
    ) -> bool:
        """Grant credits for one confirmed payment. Returns False if `(source, ref)` was already
        granted (replay), True if this call granted."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO credit_grants VALUES (?,?,?,?,?,?)",
                (source, ref, workspace_id, runs, amount_usd_cents, int(time.time())),
            )
            return cur.rowcount == 1

    def record_pending(
        self, source: str, ref: str, workspace_id: str, runs: int, amount_usd_cents: int
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO pending_purchases VALUES (?,?,?,?,?,?,'pending')",
                (source, ref, workspace_id, runs, amount_usd_cents, int(time.time())),
            )

    def settle_pending(self, source: str, ref: str) -> bool:
        """Turn a pending purchase into credits after confirmed settlement. Idempotent."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT workspace_id, runs, amount_usd_cents, state FROM pending_purchases "
                    "WHERE source=? AND ref=?",
                    (source, ref),
                ).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    return False
                workspace_id, runs, cents, _state = row
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO credit_grants VALUES (?,?,?,?,?,?)",
                    (source, ref, workspace_id, runs, cents, int(time.time())),
                )
                self._db.execute(
                    "UPDATE pending_purchases SET state='settled' WHERE source=? AND ref=?",
                    (source, ref),
                )
                self._db.execute("COMMIT")
                return cur.rowcount == 1
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def fail_pending(self, source: str, ref: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE pending_purchases SET state='failed' "
                "WHERE source=? AND ref=? AND state='pending'",
                (source, ref),
            )

    def pending(self) -> list[dict]:
        return [
            dict(
                zip(
                    ("source", "ref", "workspace_id", "runs", "amount_usd_cents", "created_at"),
                    r,
                    strict=True,
                )
            )
            for r in self._db.execute(
                "SELECT source, ref, workspace_id, runs, amount_usd_cents, created_at "
                "FROM pending_purchases WHERE state='pending' ORDER BY created_at"
            )
        ]

    def revenue(self) -> dict[str, int]:
        """Granted (i.e. confirmed-paid) cents by source. Operator metric."""
        return {
            s: c
            for s, c in self._db.execute(
                "SELECT source, SUM(amount_usd_cents) FROM credit_grants GROUP BY source"
            )
        }
