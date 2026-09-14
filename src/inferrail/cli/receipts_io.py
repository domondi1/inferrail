"""`inferrail receipts import|export`: move records between the JSONL and
SQLite receipt sinks (see docs/adr/0013-sqlite-receipts-store.md).

Neither direction is destructive: import is idempotent (re-running it
against the same JSONL file never duplicates rows in the store, since
`ReceiptsStore.emit` is `INSERT OR IGNORE` on `receipt_id`), and export
only ever appends to its target JSONL file, never truncates or
overwrites an existing one.
"""

from __future__ import annotations

from pathlib import Path

from inferrail.receipts.sqlite_store import ReceiptsStore, import_jsonl


def run_receipts_import(jsonl_path: Path, db_path: Path) -> int:
    if not jsonl_path.exists():
        print(f"No JSONL file found at {jsonl_path}.")
        return 1
    store = ReceiptsStore(db_path)
    total, skipped = import_jsonl(store, jsonl_path)
    imported = total - skipped
    print(f"Read {total} receipt(s) from {jsonl_path}, imported {imported} into {db_path}.")
    if skipped:
        print(f"Skipped {skipped} malformed/unrecognized receipt row(s).")
    return 0


def run_receipts_export(db_path: Path, jsonl_path: Path) -> int:
    if not db_path.exists():
        print(f"No SQLite receipts store found at {db_path}.")
        return 1
    store = ReceiptsStore(db_path)
    count = store.export_jsonl(jsonl_path)
    print(f"Exported {count} receipt(s) from {db_path} to {jsonl_path}.")
    return 0
