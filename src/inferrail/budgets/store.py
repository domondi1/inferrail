"""A WAL-mode SQLite store for `Budget` entities — the persistence behind
`inferrail budget set|list|rm` and, later, the local control API's
budgets CRUD (v0.3.0 unit 4).

Same connection discipline as `inferrail.receipts.sqlite_store.ReceiptsStore`
(one connection per call, WAL mode, a generous `busy_timeout`, `BEGIN
IMMEDIATE` around writes) — see that module's docstring for the full
rationale, which applies here unchanged.

Deliberately a separate file/table from receipts: budgets are a small,
rarely-written set of *limits*; receipts are a large, frequently-written
*ledger*. Mixing them into one file would couple two very different
write-volume/locking profiles for no benefit.

Reservations (`budget_reservations`) live here too, not in the receipts
file: admission must read budgets, sum outstanding reservations, and
insert a new one inside a single `BEGIN IMMEDIATE` transaction, and that
lock has to be on the same file the reservations are in. See
docs/adr/0021-atomic-budget-reservations.md.
"""

from __future__ import annotations

import builtins
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from inferrail.budgets.schema import Budget

SCHEMA = """
CREATE TABLE IF NOT EXISTS budgets (
    budget_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    scope_value TEXT,
    window TEXT NOT NULL,
    mode TEXT NOT NULL,
    limit_usd TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    project TEXT,
    work_id TEXT,
    amount_usd TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_budget_reservations_work_id ON budget_reservations(work_id);
CREATE INDEX IF NOT EXISTS idx_budget_reservations_project ON budget_reservations(project);
"""

_COLUMNS = ["budget_id", "scope", "scope_value", "window", "mode", "limit_usd", "created_at"]
_RESERVATION_COLUMNS = [
    "reservation_id", "request_id", "project", "work_id", "amount_usd", "state", "created_at",
]

ReservationState = Literal["active", "held"]


@dataclass(frozen=True)
class Reservation:
    """Dollars set aside for one provider attempt, against every budget
    whose scope matches `project`/`work_id` and whose window contains
    `created_at`. `active` while the attempt is in flight; `held` once it
    finished without a priceable cost but may have been billed. Carries
    attribution only — never request content."""

    reservation_id: str
    request_id: str
    project: str | None
    work_id: str | None
    amount_usd: Decimal
    state: ReservationState
    created_at: datetime


class AdmissionTransaction:
    """The read-check-insert unit of one admission, bound to a connection
    that already holds SQLite's write lock (see `BudgetStore.admission`)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def budgets(self) -> list[Budget]:
        rows = self._conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM budgets ORDER BY budget_id"
        ).fetchall()
        return [_row_to_budget(row) for row in rows]

    def reserved_usd(self, budget: Budget, since: datetime | None) -> Decimal:
        """Sum of outstanding (active or held) reservations this budget's
        scope covers, created at or after `since` (its window start)."""
        sql = "SELECT amount_usd FROM budget_reservations WHERE 1 = 1"
        params: list[object] = []
        if budget.scope == "project":
            sql += " AND project = ?"
            params.append(budget.scope_value)
        elif budget.scope == "work_id":
            sql += " AND work_id = ?"
            params.append(budget.scope_value)
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since.timestamp())
        rows = self._conn.execute(sql, params).fetchall()
        return sum((Decimal(row["amount_usd"]) for row in rows), Decimal(0))

    def insert(self, reservation: Reservation) -> None:
        self._conn.execute(
            f"""INSERT INTO budget_reservations ({", ".join(_RESERVATION_COLUMNS)})
                VALUES ({", ".join("?" for _ in _RESERVATION_COLUMNS)})""",
            (
                reservation.reservation_id,
                reservation.request_id,
                reservation.project,
                reservation.work_id,
                str(reservation.amount_usd),
                reservation.state,
                reservation.created_at.timestamp(),
            ),
        )


class BudgetStore:
    """CRUD over `Budget` rows in one SQLite file.

    `set` is an upsert keyed on `budget_id` (itself deterministic from
    scope/scope_value/window — see `schema.new_budget_id`), so calling
    `inferrail budget set` twice for the same scope replaces the limit
    rather than creating a second, conflicting row.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def set(self, budget: Budget) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"""INSERT INTO budgets ({", ".join(_COLUMNS)})
                    VALUES ({", ".join("?" for _ in _COLUMNS)})
                    ON CONFLICT(budget_id) DO UPDATE SET
                        scope = excluded.scope,
                        scope_value = excluded.scope_value,
                        window = excluded.window,
                        mode = excluded.mode,
                        limit_usd = excluded.limit_usd,
                        created_at = excluded.created_at""",
                _budget_to_row(budget),
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, budget_id: str) -> Budget | None:
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM budgets WHERE budget_id = ?", (budget_id,)
            ).fetchone()
        finally:
            conn.close()
        return _row_to_budget(row) if row is not None else None

    def list(self) -> list[Budget]:
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM budgets ORDER BY budget_id"
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_budget(row) for row in rows]

    def remove(self, budget_id: str) -> bool:
        """Returns whether a row was actually deleted — lets `inferrail
        budget rm` report "no such budget" instead of silently no-op'ing,
        and is idempotent to call twice (the second call returns False,
        never raises)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("DELETE FROM budgets WHERE budget_id = ?", (budget_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    @contextmanager
    def admission(self) -> Iterator[AdmissionTransaction]:
        """One atomic admission: `BEGIN IMMEDIATE` takes SQLite's write
        lock up front, so every other admission — in this process or any
        other gateway process sharing this file — waits until this one
        commits its reservation (or rolls back on a refusal). That is what
        closes the check-then-act race."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield AdmissionTransaction(conn)
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
        finally:
            conn.close()

    def release_reservation(self, reservation_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM budget_reservations WHERE reservation_id = ?", (reservation_id,)
            )
            conn.commit()
        finally:
            conn.close()

    def hold_reservation(self, reservation_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE budget_reservations SET state = 'held' WHERE reservation_id = ?",
                (reservation_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def list_reservations(self) -> builtins.list[Reservation]:
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {', '.join(_RESERVATION_COLUMNS)} FROM budget_reservations "
                "ORDER BY created_at, reservation_id"
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_reservation(row) for row in rows]


def _budget_to_row(budget: Budget) -> tuple[object, ...]:
    return (
        budget.budget_id,
        budget.scope,
        budget.scope_value,
        budget.window,
        budget.mode,
        str(budget.limit_usd),
        budget.created_at.timestamp(),
    )


def _row_to_budget(row: sqlite3.Row) -> Budget:
    return Budget(
        budget_id=row["budget_id"],
        scope=row["scope"],
        scope_value=row["scope_value"],
        window=row["window"],
        mode=row["mode"],
        limit_usd=Decimal(row["limit_usd"]),
        created_at=datetime.fromtimestamp(row["created_at"], tz=UTC),
    )


def _row_to_reservation(row: sqlite3.Row) -> Reservation:
    return Reservation(
        reservation_id=row["reservation_id"],
        request_id=row["request_id"],
        project=row["project"],
        work_id=row["work_id"],
        amount_usd=Decimal(row["amount_usd"]),
        state=row["state"],
        created_at=datetime.fromtimestamp(row["created_at"], tz=UTC),
    )
