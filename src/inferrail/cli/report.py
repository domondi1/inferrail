"""`inferrail report`: turn a local receipts JSONL file into a business-level
answer to "what is each <dimension> costing me".

Kept independent of `argparse`/stdout so the aggregation logic is testable
directly (see docs/PRINCIPLES.md's "deterministic, testable behavior") —
mirrors how `InferenceEngine` stays independent of FastAPI.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from inferrail.receipts.schema import InferenceReceipt

_UNATTRIBUTED = "(unattributed)"
_FIELD_DIMENSIONS = {"provider", "model", "route"}


@dataclass
class ReportGroup:
    key: str
    requests: int = 0
    successful_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    known_cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    unknown_cost_requests: int = 0


def _group_key(receipt: InferenceReceipt, by: str) -> str:
    if by in _FIELD_DIMENSIONS:
        return str(getattr(receipt, by))
    return receipt.attributes.get(by, _UNATTRIBUTED)


def load_receipts(path: Path) -> tuple[list[InferenceReceipt], int]:
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


def aggregate(receipts: list[InferenceReceipt], by: str) -> list[ReportGroup]:
    groups: dict[str, ReportGroup] = {}
    for receipt in receipts:
        key = _group_key(receipt, by)
        group = groups.setdefault(key, ReportGroup(key=key))
        group.requests += 1
        if receipt.status == "success":
            group.successful_requests += 1
        if receipt.prompt_tokens is not None:
            group.input_tokens += receipt.prompt_tokens
        if receipt.completion_tokens is not None:
            group.output_tokens += receipt.completion_tokens
        if receipt.estimated_cost_usd is not None:
            group.known_cost_usd += receipt.estimated_cost_usd
        elif receipt.status == "success":
            # Only a successful request with unresolvable pricing counts as
            # "unknown cost" — a failed request legitimately incurred no
            # usage, so it isn't a pricing gap.
            group.unknown_cost_requests += 1

    return sorted(groups.values(), key=lambda g: (-g.known_cost_usd, g.key))


def format_usd(value: Decimal) -> str:
    """Format a cost keeping enough precision to be meaningful.

    Individual requests routinely cost a fraction of a cent — naively
    formatting to 2 decimal places (`.2f`) would silently display real,
    known costs as `$0.00`, which is exactly the fabricated-zero failure
    mode this project refuses to produce. Shows up to 6 decimal places
    (the calculator's own precision), trimmed of trailing zeros, with a
    floor of 2 decimal places for readability on larger totals.
    """
    text = format(value.quantize(Decimal("0.000001")), "f")
    integer_part, _, frac_part = text.partition(".")
    frac_part = frac_part.rstrip("0").ljust(2, "0")
    return f"${integer_part}.{frac_part}"


def _cost_cell(group: ReportGroup) -> str:
    """The COST (USD) cell for one row.

    A group whose known-cost sum is exactly zero *because every
    contributing request has unresolvable pricing* must never render as
    `$0.00` — that reads as "this cost nothing," which is exactly the
    fabricated-zero failure mode this project refuses to produce (see
    docs/PRODUCT.md's "no fabricated metrics"). A genuine zero (no
    receipts, or receipts with real zero-token usage and known pricing)
    still renders as `$0.00`.
    """
    if group.known_cost_usd == 0 and group.unknown_cost_requests > 0:
        return "unknown"
    return format_usd(group.known_cost_usd)


def format_table(groups: list[ReportGroup], by: str) -> str:
    if not groups:
        return "No receipts to report on."

    header_label = by.upper()
    columns = [
        header_label, "REQUESTS", "INPUT TOKENS", "OUTPUT TOKENS", "COST (USD)", "UNKNOWN COST",
    ]
    rows: list[list[str]] = []

    total = ReportGroup(key="TOTAL")
    for g in groups:
        rows.append(
            [
                g.key,
                str(g.requests),
                str(g.input_tokens),
                str(g.output_tokens),
                _cost_cell(g),
                str(g.unknown_cost_requests) if g.unknown_cost_requests else "",
            ]
        )
        total.requests += g.requests
        total.input_tokens += g.input_tokens
        total.output_tokens += g.output_tokens
        total.known_cost_usd += g.known_cost_usd
        total.unknown_cost_requests += g.unknown_cost_requests

    rows.append(
        [
            "TOTAL",
            str(total.requests),
            str(total.input_tokens),
            str(total.output_tokens),
            _cost_cell(total),
            str(total.unknown_cost_requests) if total.unknown_cost_requests else "",
        ]
    )

    widths = [
        max(len(col), *(len(row[i]) for row in rows)) for i, col in enumerate(columns)
    ]

    def _format_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True))

    lines = [_format_row(columns)]
    data_rows, total_row = rows[:-1], rows[-1]
    lines.extend(_format_row(row) for row in data_rows)
    lines.append(_format_row(["-" * w for w in widths]))
    lines.append(_format_row(total_row))
    return "\n".join(lines)


def run_report(path: Path, by: str) -> int:
    if not path.exists():
        print(
            f"No receipts found at {path}. Run some requests through the "
            "gateway first (see receipts.sink in inferrail.yaml).",
            file=sys.stderr,
        )
        return 0

    receipts, skipped = load_receipts(path)
    groups = aggregate(receipts, by)
    print(format_table(groups, by))
    if skipped:
        print(f"\nSkipped {skipped} malformed/unrecognized receipt row(s).", file=sys.stderr)
    return 0
