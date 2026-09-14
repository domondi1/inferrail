"""Reading `InferenceReceipt` rows from a JSONL file.

Factored out of `inferrail.cli.report` so both the CLI reporting commands
and `inferrail.receipts.sqlite_store`'s JSONL import path share one
tolerant parser, rather than the SQLite side re-implementing (and
potentially drifting from) the same malformed-row handling.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from inferrail.receipts.schema import InferenceReceipt


def read_jsonl_receipts(path: Path) -> tuple[list[InferenceReceipt], int]:
    """Read a receipts JSONL file, tolerating malformed/older-schema rows.

    Returns `(receipts, skipped_count)`. A row that fails to parse as JSON
    or fails schema validation is skipped, not fatal — one corrupt line
    (a truncated write, a receipt from a future schema version) should
    never prevent reporting on everything else in the file.
    """
    receipts: list[InferenceReceipt] = []
    skipped = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                receipts.append(InferenceReceipt.model_validate(data))
            except (json.JSONDecodeError, ValidationError):
                skipped += 1
    return receipts, skipped
