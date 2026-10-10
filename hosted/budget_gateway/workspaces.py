"""Workspaces, governed-run metering and the credit ledger for Inferrail Hosted.

One SQLite file holds every workspace's *billing* state. Each workspace's receipts and budgets
live in their own files (see `service.py`).

**What is billed.** A *governed run* is a distinct work id in a UTC month. It becomes billable
only when at least one of its calls is answered by the provider. While its first calls are in
flight, the run *holds* one unit (a free-allowance slot, or one prepaid credit past the
allowance), so concurrent new runs can't overspend. If every call of the run fails or is refused
by its own budget before the provider answers, the hold is released and the run costs nothing.
Later calls in a billed run cost nothing more.

**Financial states (auditable; each has a test):**

| Table | State | Meaning |
|---|---|---|
| `governed_runs` | `held` | A unit reserved by in-flight calls (`inflight` > 0) |
| `governed_runs` | `billed` | At least one answered call. Permanent for the month |
| `pending_purchases` | `pending` | x402 payment seen by the handler, not yet confirmed |
| `pending_purchases` | `settled` / `failed` | Decided by the settle hook or by the chain |
| `credit_grants` | — | Credits from one confirmed payment. `UNIQUE(source, ref)`: never twice |
| `credit_reversals` | — | Refunded credits for one grant. Monotonic: a replay is a no-op |

Balance = grants − reversals − (held + billed runs that needed a credit). A refund can make the
balance negative. Paid runs are then refused until it is topped up, so a customer can never
spend credits they don't have.

Workspace API keys are stored only as SHA-256 hashes. Provider keys never reach this module.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
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
  state TEXT NOT NULL CHECK (state IN ('held', 'billed')),
  inflight INTEGER NOT NULL,
  PRIMARY KEY (workspace_id, month, work_id)
);
CREATE TABLE IF NOT EXISTS credit_grants (
  source TEXT NOT NULL,
  ref TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  runs INTEGER NOT NULL CHECK (runs > 0),
  amount_usd_cents INTEGER NOT NULL,
  external_id TEXT,
  granted_at INTEGER NOT NULL,
  PRIMARY KEY (source, ref)
);
CREATE INDEX IF NOT EXISTS credit_grants_external ON credit_grants(source, external_id);
CREATE TABLE IF NOT EXISTS credit_reversals (
  source TEXT NOT NULL,
  ref TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  runs INTEGER NOT NULL CHECK (runs >= 0),
  amount_usd_cents INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (source, ref)
);
CREATE TABLE IF NOT EXISTS pending_purchases (
  source TEXT NOT NULL,
  ref TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  runs INTEGER NOT NULL,
  amount_usd_cents INTEGER NOT NULL,
  authorizer TEXT NOT NULL,
  nonce TEXT NOT NULL,
  valid_before INTEGER NOT NULL,
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
    month: str
    paid: bool
    month_runs: int
    credits_remaining: int


@dataclass(frozen=True)
class Usage:
    month: str
    month_runs: int
    billed_runs: int
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
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()
        self.free_runs_per_month = free_runs_per_month

    def _tx(self) -> _Tx:
        return _Tx(self)

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

    def _read(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        """Reads share the writers' lock: one sqlite3 connection must never be used by two
        threads at once."""
        with self._lock:
            return list(self._db.execute(sql, params).fetchall())

    def authenticate(self, api_key: str) -> str | None:
        if not api_key.startswith(KEY_PREFIX):
            return None
        rows = self._read(
            "SELECT workspace_id FROM workspaces WHERE key_hash=? AND disabled=0",
            (hash_key(api_key),),
        )
        return str(rows[0][0]) if rows else None

    def count_workspaces(self) -> int:
        return int(self._read("SELECT COUNT(*) FROM workspaces")[0][0])  # type: ignore[call-overload]

    # -- metering -----------------------------------------------------------------------------

    def _credits_remaining(self, workspace_id: str) -> int:
        granted = self._db.execute(
            "SELECT COALESCE(SUM(runs),0) FROM credit_grants WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()[0]
        reversed_ = self._db.execute(
            "SELECT COALESCE(SUM(runs),0) FROM credit_reversals WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()[0]
        consumed = self._db.execute(
            "SELECT COUNT(*) FROM governed_runs WHERE workspace_id=? AND paid=1", (workspace_id,)
        ).fetchone()[0]
        return int(granted - reversed_ - consumed)

    def _month_runs(self, workspace_id: str, month: str) -> int:
        return int(
            self._db.execute(
                "SELECT COUNT(*) FROM governed_runs WHERE workspace_id=? AND month=?",
                (workspace_id, month),
            ).fetchone()[0]
        )

    def hold_run(self, workspace_id: str, work_id: str, now: float | None = None) -> Admission:
        """Admit one call of `work_id`. A new run takes a free slot, or a credit past the free
        allowance. Refuses (admitted=False) when neither is available. Every admitted call must
        be followed by exactly one `finish_call`."""
        now = time.time() if now is None else now
        month = month_of(now)
        with self._tx():
            runs = self._month_runs(workspace_id, month)
            credits = self._credits_remaining(workspace_id)
            known = self._db.execute(
                "SELECT paid FROM governed_runs WHERE workspace_id=? AND month=? AND work_id=?",
                (workspace_id, month, work_id),
            ).fetchone()
            if known:
                self._db.execute(
                    "UPDATE governed_runs SET inflight=inflight+1 "
                    "WHERE workspace_id=? AND month=? AND work_id=?",
                    (workspace_id, month, work_id),
                )
                return Admission(True, month, bool(known[0]), runs, credits)
            paid = runs >= self.free_runs_per_month
            if paid and credits <= 0:
                return Admission(False, month, True, runs, credits)
            self._db.execute(
                "INSERT INTO governed_runs VALUES (?,?,?,?,?,'held',1)",
                (workspace_id, month, work_id, int(now), int(paid)),
            )
            return Admission(True, month, paid, runs + 1, credits - int(paid))

    def finish_call(self, workspace_id: str, month: str, work_id: str, answered: bool) -> None:
        """Close one admitted call. An answered call bills the run. An unanswered last in-flight
        call of a never-billed run releases its hold (free slot or credit returns)."""
        with self._tx():
            if answered:
                self._db.execute(
                    "UPDATE governed_runs SET state='billed', inflight=MAX(inflight-1,0) "
                    "WHERE workspace_id=? AND month=? AND work_id=?",
                    (workspace_id, month, work_id),
                )
                return
            self._db.execute(
                "UPDATE governed_runs SET inflight=MAX(inflight-1,0) "
                "WHERE workspace_id=? AND month=? AND work_id=?",
                (workspace_id, month, work_id),
            )
            self._db.execute(
                "DELETE FROM governed_runs WHERE workspace_id=? AND month=? AND work_id=? "
                "AND state='held' AND inflight=0",
                (workspace_id, month, work_id),
            )

    def release_orphaned_holds(self) -> int:
        """At startup: a held run with no live process behind it (crash mid-call) is released.
        Billed runs are kept. Only safe when no other instance is running (single-instance
        lock)."""
        with self._tx():
            cur = self._db.execute("DELETE FROM governed_runs WHERE state='held'")
            self._db.execute("UPDATE governed_runs SET inflight=0")
            return int(cur.rowcount)

    def usage(self, workspace_id: str, now: float | None = None) -> Usage:
        with self._lock:
            month = month_of(time.time() if now is None else now)
            runs = self._month_runs(workspace_id, month)
            billed = int(
                self._db.execute(
                    "SELECT COUNT(*) FROM governed_runs WHERE workspace_id=? AND month=? "
                    "AND state='billed'",
                    (workspace_id, month),
                ).fetchone()[0]
            )
            return Usage(
                month,
                runs,
                billed,
                self.free_runs_per_month,
                max(self.free_runs_per_month - runs, 0),
                self._credits_remaining(workspace_id),
            )

    # -- credits ------------------------------------------------------------------------------

    def grant(
        self,
        source: str,
        ref: str,
        workspace_id: str,
        runs: int,
        amount_usd_cents: int,
        external_id: str | None = None,
    ) -> bool:
        """Grant credits for one confirmed payment. False if `(source, ref)` was already granted."""
        with self._tx():
            cur = self._db.execute(
                "INSERT OR IGNORE INTO credit_grants VALUES (?,?,?,?,?,?,?)",
                (source, ref, workspace_id, runs, amount_usd_cents, external_id, int(time.time())),
            )
            return cur.rowcount == 1

    def apply_refund(
        self, source: str, ref: str | None, refunded_cents: int, *, external_id: str | None = None
    ) -> int | None:
        """Reverse credits for a refunded grant, proportionally to the refunded amount. The
        reversal is monotonic (never decreases), so replays and out-of-order events are safe.
        Returns the total runs reversed for the grant, or None if no matching grant exists."""
        with self._tx():
            if ref is None:
                row = self._db.execute(
                    "SELECT ref, workspace_id, runs, amount_usd_cents FROM credit_grants "
                    "WHERE source=? AND external_id=?",
                    (source, external_id),
                ).fetchone()
            else:
                row = self._db.execute(
                    "SELECT ref, workspace_id, runs, amount_usd_cents FROM credit_grants "
                    "WHERE source=? AND ref=?",
                    (source, ref),
                ).fetchone()
            if row is None:
                return None
            grant_ref, workspace_id, runs, amount = row
            refunded = min(max(refunded_cents, 0), amount)
            target = runs if refunded >= amount else (runs * refunded) // max(amount, 1)
            self._db.execute(
                "INSERT INTO credit_reversals VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(source, ref) DO UPDATE SET "
                "runs=MAX(runs, excluded.runs), "
                "amount_usd_cents=MAX(amount_usd_cents, excluded.amount_usd_cents), "
                "updated_at=excluded.updated_at",
                (source, grant_ref, workspace_id, target, refunded, int(time.time())),
            )
            total = self._db.execute(
                "SELECT runs FROM credit_reversals WHERE source=? AND ref=?", (source, grant_ref)
            ).fetchone()[0]
            return int(total)

    def record_pending(
        self,
        source: str,
        ref: str,
        workspace_id: str,
        runs: int,
        amount_usd_cents: int,
        *,
        authorizer: str,
        nonce: str,
        valid_before: int,
    ) -> None:
        with self._tx():
            self._db.execute(
                "INSERT OR IGNORE INTO pending_purchases VALUES (?,?,?,?,?,?,?,?,?,'pending')",
                (
                    source,
                    ref,
                    workspace_id,
                    runs,
                    amount_usd_cents,
                    authorizer,
                    nonce,
                    valid_before,
                    int(time.time()),
                ),
            )

    def settle_pending(self, source: str, ref: str) -> bool:
        """Turn a pending purchase into credits after confirmed settlement. Idempotent: True only
        for the call that actually granted."""
        with self._tx():
            row = self._db.execute(
                "SELECT workspace_id, runs, amount_usd_cents FROM pending_purchases "
                "WHERE source=? AND ref=?",
                (source, ref),
            ).fetchone()
            if row is None:
                return False
            workspace_id, runs, cents = row
            cur = self._db.execute(
                "INSERT OR IGNORE INTO credit_grants VALUES (?,?,?,?,?,?,?)",
                (source, ref, workspace_id, runs, cents, None, int(time.time())),
            )
            self._db.execute(
                "UPDATE pending_purchases SET state='settled' WHERE source=? AND ref=?",
                (source, ref),
            )
            return cur.rowcount == 1

    def fail_pending(self, source: str, ref: str) -> None:
        """Mark a pending purchase failed. Never overrides a confirmed settlement."""
        with self._tx():
            self._db.execute(
                "UPDATE pending_purchases SET state='failed' "
                "WHERE source=? AND ref=? AND state='pending'",
                (source, ref),
            )

    def pending(self) -> list[dict[str, object]]:
        with self._lock:
            cols = (
                "source",
                "ref",
                "workspace_id",
                "runs",
                "amount_usd_cents",
                "authorizer",
                "nonce",
                "valid_before",
                "created_at",
            )
            return [
                dict(zip(cols, r, strict=True))
                for r in self._db.execute(
                    f"SELECT {', '.join(cols)} FROM pending_purchases "
                    "WHERE state='pending' ORDER BY created_at"
                )
            ]

    def reconcile_pending(
        self,
        authorization_used: Callable[[str, str], bool | None],
        now: float | None = None,
        grace_seconds: int = 120,
    ) -> dict[str, int]:
        """Resolve pending x402 purchases from chain truth.
        `authorization_used(authorizer, nonce)` returns True (the EIP-3009 authorization was
        used: the payment settled), False (not used), or None (unknown: RPC error).
        - Used → credits granted (idempotent).
        - Not used and past `valid_before` + grace → failed: it can never settle now.
        - Unknown, or not yet expired → left pending."""
        now = time.time() if now is None else now
        out = {"settled": 0, "failed": 0, "unresolved": 0}
        for p in self.pending():
            used = authorization_used(str(p["authorizer"]), str(p["nonce"]))
            if used is True:
                self.settle_pending(str(p["source"]), str(p["ref"]))
                out["settled"] += 1
            elif used is False and now > int(str(p["valid_before"])) + grace_seconds:
                self.fail_pending(str(p["source"]), str(p["ref"]))
                out["failed"] += 1
            else:
                out["unresolved"] += 1
        return out

    # -- reporting ----------------------------------------------------------------------------

    def statement(self, workspace_id: str) -> dict[str, object]:
        with self._lock:
            grants = [
                {"source": s, "ref": r, "runs": n, "amount_usd_cents": c, "granted_at": t}
                for s, r, n, c, t in self._db.execute(
                    "SELECT source, ref, runs, amount_usd_cents, granted_at FROM credit_grants "
                    "WHERE workspace_id=? ORDER BY granted_at",
                    (workspace_id,),
                )
            ]
            reversals = [
                {"source": s, "ref": r, "runs": n, "amount_usd_cents": c}
                for s, r, n, c in self._db.execute(
                    "SELECT source, ref, runs, amount_usd_cents FROM credit_reversals "
                    "WHERE workspace_id=?",
                    (workspace_id,),
                )
            ]
            pending = [
                {"source": s, "ref": r, "runs": n, "amount_usd_cents": c}
                for s, r, n, c in self._db.execute(
                    "SELECT source, ref, runs, amount_usd_cents FROM pending_purchases "
                    "WHERE workspace_id=? AND state='pending'",
                    (workspace_id,),
                )
            ]
            return {
                "grants": grants,
                "reversals": reversals,
                "pending_purchases": pending,
                "credits_remaining": self._credits_remaining(workspace_id),
            }

    def revenue(self) -> dict[str, int]:
        """Confirmed-paid cents by source, net of refunds. Operator metric."""
        with self._lock:
            gross = dict(
                self._db.execute(
                    "SELECT source, SUM(amount_usd_cents) FROM credit_grants GROUP BY source"
                ).fetchall()
            )
            refunds = dict(
                self._db.execute(
                    "SELECT source, SUM(amount_usd_cents) FROM credit_reversals GROUP BY source"
                ).fetchall()
            )
            return {k: int(v) - int(refunds.get(k, 0)) for k, v in gross.items()}


class _Tx:
    """`BEGIN IMMEDIATE` under the process lock: one writer at a time, all-or-nothing."""

    def __init__(self, ledger: WorkspaceLedger) -> None:
        self._l = ledger

    def __enter__(self) -> None:
        self._l._lock.acquire()
        try:
            self._l._db.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._l._lock.release()
            raise

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            self._l._db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self._l._lock.release()
