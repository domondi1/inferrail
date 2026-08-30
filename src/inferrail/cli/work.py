"""Read and write the local, evidence-backed Work Economics view."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import TypeAdapter

from inferrail.cli.report import format_usd, load_receipts
from inferrail.receipts.schema import InferenceReceipt
from inferrail.work.builder import (
    aggregate_work_summaries,
    append_outcome,
    build_work_summary,
    load_outcomes,
)
from inferrail.work.schema import WorkOutcomeRecord, WorkSummary

DEFAULT_OUTCOMES_PATH = Path("./inferrail-work-outcomes.jsonl")


def _cost_cell(summary: WorkSummary) -> str:
    if summary.known_attributed_inference_cost_usd is None:
        return "unavailable"
    return format_usd(summary.known_attributed_inference_cost_usd)


def format_work_summary(summary: WorkSummary) -> str:
    lines = [
        f"Work:                              {summary.work_id}",
        f"Customer-declared outcome:         {summary.outcome_status or 'undeclared'}",
        f"Inference execution status:         {summary.inference_status}",
        f"Inference receipts:                 {summary.receipt_count}",
        f"Known attributed inference cost:    {_cost_cell(summary)}",
        f"Unknown-cost inference receipts:    {summary.unknown_cost_count}",
    ]
    if summary.outcome_recorded_at is not None:
        lines.append(
            f"Outcome last declared:              {summary.outcome_recorded_at.isoformat()}"
        )
    if summary.started_at is not None:
        lines.append(f"Inference started:                  {summary.started_at.isoformat()}")
    if summary.ended_at is not None:
        lines.append(f"Inference ended:                    {summary.ended_at.isoformat()}")
    return "\n".join(lines)


def format_work_aggregate(summaries: list[WorkSummary]) -> str:
    if not summaries:
        return "No work evidence to report on."

    columns = [
        "WORK ID",
        "OUTCOME",
        "RECEIPTS",
        "INFERENCE",
        "KNOWN ATTRIBUTED INFERENCE COST",
        "UNKNOWN COST",
    ]
    rows = [
        [
            summary.work_id,
            summary.outcome_status or "undeclared",
            str(summary.receipt_count),
            summary.inference_status,
            _cost_cell(summary),
            str(summary.unknown_cost_count) if summary.unknown_cost_count else "",
        ]
        for summary in summaries
    ]
    widths = [
        max(len(column), *(len(row[index]) for row in rows)) for index, column in enumerate(columns)
    ]

    def format_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True))

    groups: dict[str, list[WorkSummary]] = {}
    for summary in summaries:
        groups.setdefault(summary.outcome_status or "undeclared", []).append(summary)

    outcome_columns = [
        "OUTCOME STATUS",
        "WORK UNITS",
        "FULLY-KNOWN WORKS",
        "UNKNOWN-COST WORKS",
        "NO RECEIPT EVIDENCE",
        "KNOWN ATTRIBUTED INFERENCE COST",
        "KNOWN-ONLY AVERAGE",
    ]
    outcome_rows: list[list[str]] = []
    for status, group in sorted(groups.items()):
        fully_known = [
            summary
            for summary in group
            if summary.receipt_count > 0
            and summary.unknown_cost_count == 0
            and summary.known_attributed_inference_cost_usd is not None
        ]
        no_receipt_evidence_count = sum(summary.receipt_count == 0 for summary in group)
        known_cost = sum(
            ((summary.known_attributed_inference_cost_usd or Decimal("0")) for summary in group),
            Decimal("0"),
        )
        fully_known_cost = sum(
            (
                (summary.known_attributed_inference_cost_usd or Decimal("0"))
                for summary in fully_known
            ),
            Decimal("0"),
        )
        unknown_cost_work_count = sum(summary.unknown_cost_count > 0 for summary in group)
        known_cost_cell = (
            format_usd(known_cost) if no_receipt_evidence_count != len(group) else "unavailable"
        )
        average = (
            f"{format_usd(fully_known_cost / Decimal(len(fully_known)))} (known-only)"
            if fully_known
            else "unavailable"
        )
        outcome_rows.append(
            [
                status,
                str(len(group)),
                str(len(fully_known)),
                str(unknown_cost_work_count),
                str(no_receipt_evidence_count),
                known_cost_cell,
                average,
            ]
        )

    outcome_widths = [
        max(len(column), *(len(row[index]) for row in outcome_rows))
        for index, column in enumerate(outcome_columns)
    ]

    def format_outcome_row(cells: list[str]) -> str:
        return "  ".join(
            cell.ljust(width) for cell, width in zip(cells, outcome_widths, strict=True)
        )

    lines = [format_row(columns), *(format_row(row) for row in rows)]
    lines.extend(
        [
            "",
            "WORK ECONOMICS BY CUSTOMER-DECLARED OUTCOME",
            format_outcome_row(outcome_columns),
            *(format_outcome_row(row) for row in outcome_rows),
        ]
    )
    return "\n".join(lines)


def run_work_outcome(path: Path, work_id: str, status: str) -> int:
    outcome = WorkOutcomeRecord(
        work_id=work_id, outcome_status=status, recorded_at=datetime.now(UTC)
    )
    append_outcome(path, outcome)
    print(f"Recorded outcome for work '{work_id}': {status} ({path})")
    return 0


def _load_work_evidence(
    receipts_path: Path, outcomes_path: Path
) -> tuple[list[InferenceReceipt], list[WorkOutcomeRecord], int, int]:
    receipts, skipped_receipts = load_receipts(receipts_path) if receipts_path.exists() else ([], 0)
    outcomes, skipped_outcomes = load_outcomes(outcomes_path)
    return receipts, outcomes, skipped_receipts, skipped_outcomes


def run_work(
    receipts_path: Path,
    outcomes_path: Path,
    work_id: str | None,
    *,
    all_work: bool,
    as_json: bool,
) -> int:
    receipts, outcomes, skipped_receipts, skipped_outcomes = _load_work_evidence(
        receipts_path, outcomes_path
    )
    if all_work:
        summaries = aggregate_work_summaries(receipts, outcomes)
        if as_json:
            print(TypeAdapter(list[WorkSummary]).dump_json(summaries, indent=2).decode())
        else:
            print(format_work_aggregate(summaries))
    else:
        assert work_id is not None
        summary = build_work_summary(
            work_id,
            receipts,
            [outcome for outcome in outcomes if outcome.work_id == work_id],
        )
        if summary is None:
            print(f"No work evidence found for '{work_id}'.")
        elif as_json:
            print(summary.model_dump_json(indent=2))
        else:
            print(format_work_summary(summary))
    if skipped_receipts:
        print(f"Skipped {skipped_receipts} malformed/unrecognized receipt row(s).", file=sys.stderr)
    if skipped_outcomes:
        print(f"Skipped {skipped_outcomes} malformed/unrecognized outcome row(s).", file=sys.stderr)
    return 0
