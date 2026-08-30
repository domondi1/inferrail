from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError

from inferrail.receipts.aggregation import summarize_receipts
from inferrail.receipts.schema import InferenceReceipt
from inferrail.work.schema import WorkOutcomeRecord, WorkSummary


def current_outcome_for_work(outcomes: Iterable[WorkOutcomeRecord]) -> WorkOutcomeRecord | None:
    """Return the last valid appended outcome for a work.

    Deterministic local-scope policy: the final record in append order wins.
    This intentionally does not implement distributed sequencing or event
    sourcing. It is the minimal current semantics required for a local
    append-only sink.
    """
    current: WorkOutcomeRecord | None = None
    for outcome in outcomes:
        current = outcome
    return current


def load_outcomes(path: Path) -> tuple[list[WorkOutcomeRecord], int]:
    """Read a JSONL file of WorkOutcomeRecord rows, tolerating malformed rows."""
    if not path.exists():
        return [], 0
    rows: list[WorkOutcomeRecord] = []
    skipped = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                rows.append(WorkOutcomeRecord.model_validate(data))
            except (json.JSONDecodeError, ValidationError):
                skipped += 1
    return rows, skipped


def append_outcome(path: Path, outcome: WorkOutcomeRecord) -> None:
    """Append one outcome record to a JSONL sink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (outcome.model_dump_json() + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _matching_receipts_for_work(
    work_id: str, receipts: list[InferenceReceipt]
) -> list[InferenceReceipt]:
    return [r for r in receipts if r.attributes.get("work_id") == work_id]


def build_work_summary(
    work_id: str,
    receipts: list[InferenceReceipt],
    outcomes: list[WorkOutcomeRecord],
) -> WorkSummary | None:
    """Build the derived work-level summary for a given work id.

    The work remains visible even if there are only receipts or only outcome
    evidence. A work with no receipts and no outcome is not created by this
    helper; the caller can choose to show an empty result or a list of known
    work ids from the union of evidence sets.
    """
    matching = _matching_receipts_for_work(work_id, receipts)
    if not matching and not outcomes:
        return None
    economics = summarize_receipts(matching)

    current = current_outcome_for_work(outcomes)
    outcome_status = None
    outcome_recorded_at = None
    if current is not None:
        outcome_status = current.outcome_status
        outcome_recorded_at = current.recorded_at

    return WorkSummary(
        work_id=work_id,
        outcome_status=outcome_status,
        outcome_recorded_at=outcome_recorded_at,
        started_at=economics.started_at,
        ended_at=economics.ended_at,
        receipt_count=len(matching),
        known_attributed_inference_cost_usd=(economics.known_cost_usd if matching else None),
        unknown_cost_count=economics.unknown_cost_count,
        inference_status=economics.status,
    )


def _work_ids_in_receipts(receipts: list[InferenceReceipt]) -> set[str]:
    return {r.attributes["work_id"] for r in receipts if "work_id" in r.attributes}


def _work_ids_in_outcomes(outcomes: list[WorkOutcomeRecord]) -> set[str]:
    return {o.work_id for o in outcomes}


def aggregate_work_summaries(
    receipts: list[InferenceReceipt],
    outcomes: list[WorkOutcomeRecord],
) -> list[WorkSummary]:
    """Return the union of work ids seen in receipts or outcomes.

    This is intentionally not a state machine: it surfaces evidence
    completeness without dropping partially-known work.
    """
    all_work_ids = _work_ids_in_receipts(receipts) | _work_ids_in_outcomes(outcomes)
    summaries: list[WorkSummary] = []
    for work_id in sorted(all_work_ids):
        matching_receipts = _matching_receipts_for_work(work_id, receipts)
        matching_outcomes = [o for o in outcomes if o.work_id == work_id]
        summary = build_work_summary(work_id, matching_receipts, matching_outcomes)
        if summary is not None:
            summaries.append(summary)
    return summaries
