"""Durable, work_id-keyed persistence and idempotency store for AP
invoice-exception recovery.

SQLite, one file per deployment/customer -- same transactional
discipline as `hosted/work_economics/store.py`'s `DurablePurchaseStore`:
every mutating operation runs inside a single `BEGIN IMMEDIATE`
transaction so concurrent writers serialize on the database file rather
than racing an in-memory structure. Amounts are stored as canonical
strings, never float.

`work_id` is the idempotency key for the checkpoint decision: a repeated
decision request for the same `work_id` returns the stored decision
without re-deciding or re-invoking the retry adapter. If a retry adapter
call is interrupted after invocation but before its result is durably
recorded, the decision carries a `worker_id`/`lease_expires_at` lease
(set only while `status == 'retry_in_progress'`) -- once that lease
expires, `find_stale_retry_leases`/`reap_stale_retry_lease` provide the
durable recovery path: the case moves to `awaiting_human_review` with a
synthetic `ambiguous` retry attempt recorded, never silently retried
again and never assumed successful (see `engine.RecoveryEngine.
reap_stale_retries`).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from .models import DecisionStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    work_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    checkpoint_attempt_id TEXT NOT NULL,
    failure_type TEXT NOT NULL,
    confidence TEXT,
    cost_so_far_usd TEXT,
    policy_name TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    recommended_action TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    worker_id TEXT,
    lease_expires_at REAL
);

CREATE TABLE IF NOT EXISTS retry_attempts (
    attempt_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    status TEXT NOT NULL,
    cost_usd TEXT,
    confidence TEXT,
    provider TEXT NOT NULL,
    validation_passed TEXT,
    validator_version TEXT,
    created_at REAL NOT NULL,
    pre_flight_estimate_usd TEXT,
    FOREIGN KEY (work_id) REFERENCES decisions(work_id)
);

CREATE TABLE IF NOT EXISTS handoffs (
    work_id TEXT PRIMARY KEY,
    handoff_ref TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (work_id) REFERENCES decisions(work_id)
);

CREATE TABLE IF NOT EXISTS review_outcomes (
    work_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    timestamp REAL NOT NULL,
    source TEXT NOT NULL,
    correction_delta_usd TEXT,
    review_cost_usd TEXT,
    recorded_at REAL NOT NULL,
    FOREIGN KEY (work_id) REFERENCES decisions(work_id)
);

CREATE TABLE IF NOT EXISTS late_retry_results (
    work_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    status TEXT NOT NULL,
    cost_usd TEXT,
    provider TEXT NOT NULL,
    detail TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    FOREIGN KEY (work_id) REFERENCES decisions(work_id)
);
"""

# Additive migrations for databases created before a given column/table
# existed -- each statement is idempotent-by-construction (a duplicate
# column/table error is the only expected failure, swallowed below).
# See `hosted/ap_exceptions/README.md`'s "Rollback" section: this store's
# schema is additive-only by design.
_MIGRATIONS = (
    "ALTER TABLE decisions ADD COLUMN worker_id TEXT",
    "ALTER TABLE decisions ADD COLUMN lease_expires_at REAL",
    "ALTER TABLE retry_attempts ADD COLUMN pre_flight_estimate_usd TEXT",
)

_DECISION_COLUMNS = [
    "work_id", "decision_id", "checkpoint_attempt_id", "failure_type",
    "confidence", "cost_so_far_usd", "policy_name", "policy_version",
    "recommended_action", "reason", "status", "created_at",
    "worker_id", "lease_expires_at",
]
_ATTEMPT_COLUMNS = [
    "attempt_id", "work_id", "status", "cost_usd", "confidence", "provider",
    "validation_passed", "validator_version", "created_at",
    "pre_flight_estimate_usd",
]
_HANDOFF_COLUMNS = ["work_id", "handoff_ref", "created_at"]
_OUTCOME_COLUMNS = [
    "work_id", "outcome", "timestamp", "source", "correction_delta_usd",
    "review_cost_usd", "recorded_at",
]
_LATE_RESULT_COLUMNS = [
    "work_id", "attempt_id", "status", "cost_usd", "provider", "detail",
    "recorded_at",
]


class AmbiguousRetryError(RuntimeError):
    """Raised when a caller tries to record a second retry attempt for a
    work_id that already has one -- this release permits at most one
    retry per case; see `product/ap-invoice-exception-recovery.md`
    (private repo)."""


class RecoveryStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(SCHEMA)
            for stmt in _MIGRATIONS:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column already exists -- database predates this migration
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | tuple[Any, ...], columns: list[str]) -> dict[str, Any]:
        return dict(zip(columns, row, strict=True))

    def get_decision(self, work_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM decisions WHERE work_id = ?", (work_id,)
            ).fetchone()
            return self._row_to_dict(row, _DECISION_COLUMNS) if row is not None else None
        finally:
            conn.close()

    def create_decision(
        self,
        *,
        work_id: str,
        decision_id: str,
        checkpoint_attempt_id: str,
        failure_type: str,
        confidence: str | None,
        cost_so_far_usd: str | None,
        policy_name: str,
        policy_version: str,
        recommended_action: str,
        reason: str,
        status: str,
        worker_id: str | None = None,
        lease_expires_at: float | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Idempotent on `work_id`: if a decision already exists for this
        work_id, returns the existing row unchanged and `newly_created=False`
        -- the recommendation is never recomputed and no adapter is ever
        re-invoked for an already-decided work_id.

        `worker_id`/`lease_expires_at` are only meaningful while
        `status == 'retry_in_progress'` -- they let a crashed worker's
        claim on a work_id be detected and reaped later (see
        `find_stale_retry_leases`/`reap_stale_retry_lease`)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM decisions WHERE work_id = ?", (work_id,)
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return self._row_to_dict(existing, _DECISION_COLUMNS), False
            conn.execute(
                """INSERT INTO decisions
                   (work_id, decision_id, checkpoint_attempt_id, failure_type,
                    confidence, cost_so_far_usd, policy_name, policy_version,
                    recommended_action, reason, status, created_at,
                    worker_id, lease_expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    work_id, decision_id, checkpoint_attempt_id, failure_type,
                    confidence, cost_so_far_usd, policy_name, policy_version,
                    recommended_action, reason, status, time.time(),
                    worker_id, lease_expires_at,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM decisions WHERE work_id = ?", (work_id,)
            ).fetchone()
            assert row is not None
            return self._row_to_dict(row, _DECISION_COLUMNS), True
        finally:
            conn.close()

    def set_decision_status(self, work_id: str, status: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE decisions SET status = ? WHERE work_id = ?", (status, work_id))
            conn.commit()
        finally:
            conn.close()

    def record_retry_attempt(
        self,
        *,
        work_id: str,
        attempt_id: str,
        status: str,
        cost_usd: str | None,
        confidence: str | None,
        provider: str,
        validation_passed: str | None = None,
        validator_version: str | None = None,
        pre_flight_estimate_usd: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Idempotent on `attempt_id`. Raises `AmbiguousRetryError` if a
        *different* retry attempt already exists for this `work_id` --
        this release permits exactly one retry per case; a second,
        distinct attempt_id for the same work_id is a programming error
        in the caller, never silently accepted. (This is also the guard
        that surfaces when a "dead" worker's real result arrives after
        `reap_stale_retry_lease` already recorded a synthetic one for the
        same work_id -- see `engine.RecoveryEngine._execute_retry`.)"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing_for_attempt = conn.execute(
                "SELECT * FROM retry_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if existing_for_attempt is not None:
                conn.rollback()
                return self._row_to_dict(existing_for_attempt, _ATTEMPT_COLUMNS), False

            existing_for_work = conn.execute(
                "SELECT * FROM retry_attempts WHERE work_id = ?", (work_id,)
            ).fetchone()
            if existing_for_work is not None:
                conn.rollback()
                raise AmbiguousRetryError(
                    f"work_id={work_id!r} already has a recorded retry attempt "
                    f"({existing_for_work[0]!r}) -- at most one retry is permitted"
                )

            conn.execute(
                """INSERT INTO retry_attempts
                   (attempt_id, work_id, status, cost_usd, confidence, provider,
                    validation_passed, validator_version, created_at,
                    pre_flight_estimate_usd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    attempt_id, work_id, status, cost_usd, confidence, provider,
                    validation_passed, validator_version, time.time(),
                    pre_flight_estimate_usd,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM retry_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            assert row is not None
            return self._row_to_dict(row, _ATTEMPT_COLUMNS), True
        finally:
            conn.close()

    def get_retry_attempt(self, work_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM retry_attempts WHERE work_id = ?", (work_id,)
            ).fetchone()
            return self._row_to_dict(row, _ATTEMPT_COLUMNS) if row is not None else None
        finally:
            conn.close()

    def clear_retry_lease(self, work_id: str) -> None:
        """Clears any lease on a decision -- called from every terminal
        transition in `engine.RecoveryEngine._execute_retry` (success,
        validation-failed-fallback, ambiguous-exception) so a completed
        decision is never mistakenly reaped later."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE decisions SET worker_id = NULL, lease_expires_at = NULL "
                "WHERE work_id = ?",
                (work_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def find_stale_retry_leases(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Decisions still `retry_in_progress` whose lease has expired --
        candidates for `reap_stale_retry_lease`. A decision with no lease
        (`lease_expires_at IS NULL`) is never considered stale here: that
        shape only occurs for a decision created before this migration,
        and is left alone rather than guessed at."""
        ts = now if now is not None else time.time()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE status = ? "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                (DecisionStatus.RETRY_IN_PROGRESS.value, ts),
            ).fetchall()
            return [self._row_to_dict(r, _DECISION_COLUMNS) for r in rows]
        finally:
            conn.close()

    def reap_stale_retry_lease(
        self, work_id: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        """Recovers one work_id whose retry lease has expired: records a
        synthetic `ambiguous` retry attempt (provider `lease_reaper`) and
        moves the decision to `awaiting_human_review`. Never re-invokes
        the customer's retry adapter -- this is a pure store-level status
        transition; the caller's `RecoveryEngine` is responsible for any
        resulting handoff (see `RecoveryEngine.reap_stale_retries`).

        Idempotent: returns `None` (no-op) if the decision is no longer
        `retry_in_progress`, its lease isn't actually stale relative to
        `now`, or a real retry attempt already exists for this work_id
        (the "dead" worker actually finished between the staleness scan
        and this call -- never overwritten or duplicated)."""
        ts = now if now is not None else time.time()
        attempt_id = f"ret_reaped_{work_id}"
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            decision = conn.execute(
                "SELECT * FROM decisions WHERE work_id = ?", (work_id,)
            ).fetchone()
            if decision is None:
                conn.rollback()
                return None
            decision_dict = self._row_to_dict(decision, _DECISION_COLUMNS)
            if (
                decision_dict["status"] != DecisionStatus.RETRY_IN_PROGRESS.value
                or decision_dict["lease_expires_at"] is None
                or decision_dict["lease_expires_at"] >= ts
            ):
                conn.rollback()
                return None
            if conn.execute(
                "SELECT 1 FROM retry_attempts WHERE work_id = ?", (work_id,)
            ).fetchone() is not None:
                conn.rollback()
                return None

            conn.execute(
                """INSERT INTO retry_attempts
                   (attempt_id, work_id, status, cost_usd, confidence, provider,
                    validation_passed, validator_version, created_at,
                    pre_flight_estimate_usd)
                   VALUES (?, ?, ?, NULL, NULL, ?, NULL, NULL, ?, NULL)""",
                (attempt_id, work_id, "ambiguous", "lease_reaper", time.time()),
            )
            conn.execute(
                "UPDATE decisions SET status = ?, worker_id = NULL, "
                "lease_expires_at = NULL WHERE work_id = ?",
                (DecisionStatus.AWAITING_HUMAN_REVIEW.value, work_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM retry_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            assert row is not None
            return self._row_to_dict(row, _ATTEMPT_COLUMNS)
        finally:
            conn.close()

    def record_late_retry_result(
        self,
        *,
        work_id: str,
        attempt_id: str,
        status: str,
        cost_usd: str | None,
        provider: str,
        detail: str,
    ) -> None:
        """Append-only audit record for a real retry result that arrived
        after this work_id's lease was already reaped (see
        `engine.RecoveryEngine._execute_retry`) -- the decision's
        authoritative status is never silently flipped back to
        `retry_resolved` on this path, but the real cost/outcome is never
        lost either."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO late_retry_results
                   (work_id, attempt_id, status, cost_usd, provider, detail, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (work_id, attempt_id, status, cost_usd, provider, detail, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_late_retry_results(self, work_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM late_retry_results WHERE work_id = ? ORDER BY recorded_at",
                (work_id,),
            ).fetchall()
            return [self._row_to_dict(r, _LATE_RESULT_COLUMNS) for r in rows]
        finally:
            conn.close()

    def record_handoff(self, *, work_id: str, handoff_ref: str) -> tuple[dict[str, Any], bool]:
        """Idempotent on `work_id`: a repeated handoff request for an
        already-handed-off work_id returns the existing handoff_ref
        rather than calling the customer's handoff callback again."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM handoffs WHERE work_id = ?", (work_id,)
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return self._row_to_dict(existing, _HANDOFF_COLUMNS), False
            conn.execute(
                "INSERT INTO handoffs (work_id, handoff_ref, created_at) VALUES (?, ?, ?)",
                (work_id, handoff_ref, time.time()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM handoffs WHERE work_id = ?", (work_id,)
            ).fetchone()
            assert row is not None
            return self._row_to_dict(row, _HANDOFF_COLUMNS), True
        finally:
            conn.close()

    def get_handoff(self, work_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM handoffs WHERE work_id = ?", (work_id,)
            ).fetchone()
            return self._row_to_dict(row, _HANDOFF_COLUMNS) if row is not None else None
        finally:
            conn.close()

    def record_outcome(
        self,
        *,
        work_id: str,
        outcome: str,
        timestamp: float,
        source: str,
        correction_delta_usd: str | None,
        review_cost_usd: str | None,
    ) -> dict[str, Any]:
        """Append-only: every outcome revision is preserved (a later
        correction never overwrites an earlier one) -- see
        `report.build_live_report` for how the *latest* row is picked as
        authoritative while earlier ones stay available as history,
        mirroring the superseded prototype's `review_history` discipline."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM decisions WHERE work_id = ?", (work_id,)
            ).fetchone() is None:
                conn.rollback()
                raise KeyError(f"no decision recorded for work_id={work_id!r}")
            conn.execute(
                """INSERT INTO review_outcomes
                   (work_id, outcome, timestamp, source, correction_delta_usd,
                    review_cost_usd, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    work_id, outcome, timestamp, source, correction_delta_usd,
                    review_cost_usd, time.time(),
                ),
            )
            conn.execute(
                "UPDATE decisions SET status = ? WHERE work_id = ?",
                (DecisionStatus.RESOLVED.value, work_id),
            )
            conn.commit()
            rows = conn.execute(
                "SELECT * FROM review_outcomes WHERE work_id = ? ORDER BY recorded_at",
                (work_id,),
            ).fetchall()
            return self._row_to_dict(rows[-1], _OUTCOME_COLUMNS)
        finally:
            conn.close()

    def get_outcome_history(self, work_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM review_outcomes WHERE work_id = ? ORDER BY recorded_at",
                (work_id,),
            ).fetchall()
            return [self._row_to_dict(r, _OUTCOME_COLUMNS) for r in rows]
        finally:
            conn.close()

    def all_work_ids(self) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT work_id FROM decisions ORDER BY created_at").fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def delete_work_id(self, work_id: str) -> bool:
        """Retention/deletion: permanently removes every record (decision,
        retry attempt, handoff, outcome history) for one `work_id`. Returns
        `True` if a decision existed and was deleted, `False` if there was
        nothing to delete. Irreversible -- there is no soft-delete or undo
        at this layer; a caller wanting an audit trail of the deletion
        itself must keep that outside this store."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existed = conn.execute(
                "SELECT 1 FROM decisions WHERE work_id = ?", (work_id,)
            ).fetchone() is not None
            conn.execute("DELETE FROM late_retry_results WHERE work_id = ?", (work_id,))
            conn.execute("DELETE FROM review_outcomes WHERE work_id = ?", (work_id,))
            conn.execute("DELETE FROM handoffs WHERE work_id = ?", (work_id,))
            conn.execute("DELETE FROM retry_attempts WHERE work_id = ?", (work_id,))
            conn.execute("DELETE FROM decisions WHERE work_id = ?", (work_id,))
            conn.commit()
            return existed
        finally:
            conn.close()
