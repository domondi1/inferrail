from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from inferrail.receipts.schema import InferenceReceipt
from inferrail.work.builder import (
    aggregate_work_summaries,
    append_outcome,
    build_work_summary,
    current_outcome_for_work,
    load_outcomes,
)
from inferrail.work.schema import WorkOutcomeRecord, WorkSummary

_T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _receipt(**overrides: object) -> InferenceReceipt:
    defaults: dict[str, object] = dict(
        receipt_id="ir_x",
        request_id="req_x",
        route="default",
        provider="openai",
        model="gpt-4o-mini",
        status="success",
        total_latency_ms=10.0,
        timestamp=_T0,
    )
    defaults.update(overrides)
    return InferenceReceipt(**defaults)  # type: ignore[arg-type]


def test_last_appended_outcome_wins_for_same_work() -> None:
    first = WorkOutcomeRecord(work_id="work-42", outcome_status="escalated", recorded_at=_T0)
    second = WorkOutcomeRecord(work_id="work-42", outcome_status="resolved", recorded_at=_T0)

    assert current_outcome_for_work([first, second]) == second


def test_work_summary_joins_receipts_and_outcome() -> None:
    receipts = [
        _receipt(
            receipt_id="ir_1",
            attributes={"work_id": "work-42"},
            estimated_cost_usd=Decimal("0.450000"),
        ),
        _receipt(
            receipt_id="ir_2",
            attributes={"work_id": "work-42"},
            estimated_cost_usd=Decimal("0.070000"),
        ),
    ]
    outcomes = [WorkOutcomeRecord(work_id="work-42", outcome_status="resolved", recorded_at=_T0)]

    summary = build_work_summary("work-42", receipts, outcomes)

    assert summary is not None
    assert summary.work_id == "work-42"
    assert summary.known_attributed_inference_cost_usd == Decimal("0.520000")
    assert summary.outcome_status == "resolved"
    assert summary.receipt_count == 2


def test_work_summary_keeps_receipts_without_outcome_visible() -> None:
    receipts = [_receipt(attributes={"work_id": "work-42"}, estimated_cost_usd=Decimal("0.200000"))]

    summary = build_work_summary("work-42", receipts, [])

    assert summary is not None
    assert summary.outcome_status is None
    assert summary.known_attributed_inference_cost_usd == Decimal("0.200000")


def test_outcome_without_receipts_is_not_zero_cost() -> None:
    outcomes = [WorkOutcomeRecord(work_id="work-99", outcome_status="failed", recorded_at=_T0)]

    summary = build_work_summary("work-99", [], outcomes)

    assert summary is not None
    assert summary.receipt_count == 0
    assert summary.outcome_status == "failed"
    assert summary.known_attributed_inference_cost_usd is None
    assert summary.inference_status == "unknown"


def test_outcome_recorded_before_receipts_joins_later_receipt_evidence(tmp_path) -> None:
    path = tmp_path / "outcomes.jsonl"
    outcome = WorkOutcomeRecord(work_id="work-later", outcome_status="resolved", recorded_at=_T0)
    append_outcome(path, outcome)
    outcomes, skipped = load_outcomes(path)

    summary = build_work_summary(
        "work-later",
        [_receipt(attributes={"work_id": "work-later"}, estimated_cost_usd=Decimal("0.250000"))],
        outcomes,
    )

    assert skipped == 0
    assert summary is not None
    assert summary.outcome_status == "resolved"
    assert summary.known_attributed_inference_cost_usd == Decimal("0.250000")


def test_aggregate_work_summaries_uses_union_of_receipt_and_outcome_ids() -> None:
    receipts = [
        _receipt(
            receipt_id="ir_1",
            attributes={"work_id": "work-a"},
            estimated_cost_usd=Decimal("0.100000"),
        ),
        _receipt(
            receipt_id="ir_2",
            attributes={"work_id": "work-b"},
            estimated_cost_usd=Decimal("0.300000"),
        ),
    ]
    outcomes = [WorkOutcomeRecord(work_id="work-c", outcome_status="resolved", recorded_at=_T0)]

    summaries = aggregate_work_summaries(receipts, outcomes)

    keys = {s.work_id for s in summaries}
    assert keys == {"work-a", "work-b", "work-c"}


def test_append_outcome_writes_jsonl_line(tmp_path) -> None:
    path = tmp_path / "outcomes.jsonl"
    outcome = WorkOutcomeRecord(work_id="work-5", outcome_status="resolved", recorded_at=_T0)

    append_outcome(path, outcome)
    loaded, skipped = load_outcomes(path)

    assert skipped == 0
    assert len(loaded) == 1
    assert loaded[0].work_id == "work-5"
    assert loaded[0].outcome_status == "resolved"


def test_outcome_history_is_retained_and_last_valid_append_wins(tmp_path) -> None:
    path = tmp_path / "outcomes.jsonl"
    first = WorkOutcomeRecord(work_id="work-42", outcome_status="escalated", recorded_at=_T0)
    second = WorkOutcomeRecord(work_id="work-42", outcome_status="resolved", recorded_at=_T0)

    append_outcome(path, first)
    append_outcome(path, second)
    loaded, skipped = load_outcomes(path)

    assert skipped == 0
    assert [outcome.outcome_status for outcome in loaded] == ["escalated", "resolved"]
    assert current_outcome_for_work(loaded) == second


def test_work_summary_uses_shared_unknown_cost_and_time_boundaries() -> None:
    later = _T0.replace(minute=1)
    receipts = [
        _receipt(
            receipt_id="ir_known",
            timestamp=later,
            attributes={"work_id": "work-1"},
            estimated_cost_usd=Decimal("0.100000"),
        ),
        _receipt(receipt_id="ir_unknown", attributes={"work_id": "work-1"}),
    ]

    summary = build_work_summary("work-1", receipts, [])

    assert summary is not None
    assert summary.known_attributed_inference_cost_usd == Decimal("0.100000")
    assert summary.unknown_cost_count == 1
    assert summary.inference_status == "success"
    assert summary.started_at == _T0
    assert summary.ended_at == later


def test_execution_status_is_distinct_from_customer_declared_outcome() -> None:
    receipt = _receipt(status="error", attributes={"work_id": "work-2"})
    outcome = WorkOutcomeRecord(work_id="work-2", outcome_status="resolved", recorded_at=_T0)

    summary = build_work_summary("work-2", [receipt], [outcome])

    assert summary is not None
    assert summary.inference_status == "error"
    assert summary.outcome_status == "resolved"


def test_malformed_outcome_row_is_skipped_without_hiding_valid_history(tmp_path) -> None:
    path = tmp_path / "outcomes.jsonl"
    good = WorkOutcomeRecord(work_id="work-3", outcome_status="resolved", recorded_at=_T0)
    path.write_text(good.model_dump_json() + "\nnot valid json\n")

    loaded, skipped = load_outcomes(path)

    assert loaded == [good]
    assert skipped == 1


def test_work_outcome_schema_has_no_payload_fields() -> None:
    forbidden = {"prompt", "response", "messages", "content", "payload", "outcome_payload"}

    assert not (set(WorkOutcomeRecord.model_fields) & forbidden)
    assert not (set(WorkSummary.model_fields) & forbidden)
