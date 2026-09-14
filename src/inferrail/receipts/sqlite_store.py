"""A WAL-mode SQLite sink for `InferenceReceipt` records — a first-class
alternative to `sinks.JSONLReceiptSink`, not a replacement for it (see
docs/adr/0013-sqlite-receipts-store.md).

Same transactional discipline as `inferrail.ap.store.RecoveryStore` and
`hosted/a2a_economic_authority`'s stores: one connection per call (never
held open across calls), WAL journal mode, a generous `busy_timeout` so
concurrent writers (multiple gateway workers sharing one receipts file)
serialize on the database file rather than racing or erroring, and
`BEGIN IMMEDIATE` around the one write path so a write transaction is
never silently upgraded from a read lock partway through.

`receipt_id` is the primary key and `emit` is idempotent on it (`INSERT
OR IGNORE`) — replaying the same receipt (e.g. a caller retrying a JSONL
import, or a gateway worker that isn't sure whether its own prior write
landed) never duplicates a row or raises.

`work_id` and `project` are pulled out of `InferenceReceipt.attributes`
into their own indexed columns at write time, purely so they can be
indexed and queried efficiently — `attributes` itself is still stored in
full (as JSON) so every other attribution key round-trips exactly, and
so `work_id`/`project` are never a second, divergent source of truth:
`_row_to_receipt` reconstructs `attributes` from the stored JSON alone,
never from the `work_id`/`project` columns.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from inferrail.receipts.jsonl_io import read_jsonl_receipts
from inferrail.receipts.schema import InferenceReceipt, PricingSnapshot
from inferrail.receipts.sinks import JSONLReceiptSink

SQLITE_MAGIC = b"SQLite format 3\x00"

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    ts REAL NOT NULL,
    route TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    pricing_json TEXT,
    estimated_cost_usd TEXT,
    attributes_json TEXT NOT NULL,
    work_id TEXT,
    project TEXT,
    total_latency_ms REAL NOT NULL,
    retry_count INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receipts_ts ON receipts(ts);
CREATE INDEX IF NOT EXISTS idx_receipts_work_id ON receipts(work_id);
CREATE INDEX IF NOT EXISTS idx_receipts_project ON receipts(project);
CREATE INDEX IF NOT EXISTS idx_receipts_model ON receipts(model);
"""

_COLUMNS = [
    "receipt_id", "request_id", "ts", "route", "provider", "model", "status",
    "prompt_tokens", "completion_tokens", "pricing_json", "estimated_cost_usd",
    "attributes_json", "work_id", "project", "total_latency_ms", "retry_count",
]


def looks_like_sqlite(path: Path) -> bool:
    """Sniffs the file's own magic bytes rather than trusting its
    extension — lets callers (see `inferrail.cli.report.load_receipts`)
    accept whichever sink actually produced a given `--receipts` path
    without requiring a separate flag to say which one it is."""
    try:
        with path.open("rb") as f:
            return f.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


class ReceiptsStore:
    """One SQLite file holding every `InferenceReceipt` a process (or
    several, sharing this file) has emitted. Implements the `ReceiptSink`
    protocol (`sinks.ReceiptSink`) via `emit`, so `sinks.build_receipt_sink`
    can return one directly for `receipts.sink: sqlite`."""

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

    def emit(self, receipt: InferenceReceipt) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"""INSERT OR IGNORE INTO receipts ({", ".join(_COLUMNS)})
                    VALUES ({", ".join("?" for _ in _COLUMNS)})""",
                _receipt_to_row(receipt),
            )
            conn.commit()
        finally:
            conn.close()

    def read_all(self) -> tuple[list[InferenceReceipt], int]:
        """Returns `(receipts, skipped_count)` — the same shape as
        `jsonl_io.read_jsonl_receipts`, so `cli.report.load_receipts` can
        treat either sink identically. A row that fails to reconstruct
        (e.g. `attributes_json` hand-edited into invalid JSON) is skipped,
        not fatal, for the same reason a malformed JSONL line is."""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM receipts ORDER BY ts"
            ).fetchall()
        finally:
            conn.close()
        receipts: list[InferenceReceipt] = []
        skipped = 0
        for row in rows:
            try:
                receipts.append(_row_to_receipt(row))
            except (json.JSONDecodeError, ValueError, ValidationError, InvalidOperation):
                skipped += 1
        return receipts, skipped

    def query(
        self,
        *,
        work_id: str | None = None,
        project: str | None = None,
        model: str | None = None,
        since: float | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[InferenceReceipt]:
        """Indexed lookup by any combination of `work_id`/`project`/
        `model` (the three non-`ts` indexed columns) and/or `since` (a
        `ts` lower bound, exclusive — also indexed). Omit all four for
        the same result as `read_all` minus the skip count.

        `limit`/`offset` page through the (still `ts`-ordered) result —
        added for `localapi.routes`'s paginated receipts endpoint and
        its SSE tail (`since` polling), so both build on this one query
        path rather than each re-deriving their own SQL."""
        clauses: list[str] = []
        params: list[object] = []
        for column, value in (("work_id", work_id), ("project", project), ("model", model)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since is not None:
            clauses.append("ts > ?")
            params.append(since)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM receipts{where} "
                f"ORDER BY ts{limit_sql}",
                params,
            ).fetchall()
        finally:
            conn.close()
        results = []
        for row in rows:
            try:
                results.append(_row_to_receipt(row))
            except (json.JSONDecodeError, ValueError, ValidationError, InvalidOperation):
                continue
        return results

    def count(
        self,
        *,
        work_id: str | None = None,
        project: str | None = None,
        model: str | None = None,
    ) -> int:
        """Total matching rows for the same filters `query()` accepts
        (minus `since`/`limit`/`offset`, which don't affect a total) —
        lets a paginated caller report `total` without fetching every
        row just to `len()` it."""
        clauses: list[str] = []
        params: list[object] = []
        for column, value in (("work_id", work_id), ("project", project), ("model", model)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM receipts{where}", params).fetchone()
        finally:
            conn.close()
        return int(row[0])

    def export_jsonl(self, path: str | Path) -> int:
        """Writes every stored receipt to `path` as JSONL, via the same
        `JSONLReceiptSink` the JSONL sink itself uses (one atomic
        `O_APPEND` write per line). Returns the number of rows written;
        rows that failed to reconstruct are silently excluded, matching
        `read_all`'s tolerant-read behavior."""
        receipts, _skipped = self.read_all()
        sink = JSONLReceiptSink(path)
        for receipt in receipts:
            sink.emit(receipt)
        return len(receipts)


def import_jsonl(store: ReceiptsStore, jsonl_path: str | Path) -> tuple[int, int]:
    """Reads `jsonl_path` and emits every valid row into `store`. Returns
    `(read_count, skipped_count)` from the JSONL parse — safe to re-run
    against the same file or store: `emit`'s `INSERT OR IGNORE` on
    `receipt_id` means an already-imported receipt is never duplicated."""
    receipts, skipped = read_jsonl_receipts(Path(jsonl_path))
    for receipt in receipts:
        store.emit(receipt)
    return len(receipts), skipped


def _receipt_to_row(receipt: InferenceReceipt) -> tuple[Any, ...]:
    return (
        receipt.receipt_id,
        receipt.request_id,
        receipt.timestamp.timestamp(),
        receipt.route,
        receipt.provider,
        receipt.model,
        receipt.status,
        receipt.prompt_tokens,
        receipt.completion_tokens,
        receipt.pricing.model_dump_json() if receipt.pricing is not None else None,
        str(receipt.estimated_cost_usd) if receipt.estimated_cost_usd is not None else None,
        json.dumps(receipt.attributes),
        receipt.attributes.get("work_id"),
        receipt.attributes.get("project"),
        receipt.total_latency_ms,
        receipt.retry_count,
    )


def _row_to_receipt(row: sqlite3.Row) -> InferenceReceipt:
    pricing = (
        PricingSnapshot.model_validate_json(row["pricing_json"])
        if row["pricing_json"] is not None
        else None
    )
    cost = (
        Decimal(row["estimated_cost_usd"]) if row["estimated_cost_usd"] is not None else None
    )
    return InferenceReceipt(
        receipt_id=row["receipt_id"],
        request_id=row["request_id"],
        timestamp=datetime.fromtimestamp(row["ts"], tz=UTC),
        route=row["route"],
        provider=row["provider"],
        model=row["model"],
        status=row["status"],
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        pricing=pricing,
        estimated_cost_usd=cost,
        attributes=json.loads(row["attributes_json"]),
        total_latency_ms=row["total_latency_ms"],
        retry_count=row["retry_count"],
    )
