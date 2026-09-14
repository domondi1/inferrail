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
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

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
"""

_COLUMNS = ["budget_id", "scope", "scope_value", "window", "mode", "limit_usd", "created_at"]


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
