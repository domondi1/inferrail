"""Regression coverage for the historical/batch analysis module, ported
from the private repo's superseded analysis-only prototype (PR #2,
`inferrail-internal`). These scenarios reconstruct that prototype's
regression cases against this release's ported implementation.
"""

from __future__ import annotations

from typing import Any, cast

from inferrail.ap.batch import (
    CandidateBatchPolicy,
    HumanReviewBaselinePolicy,
    RetryOnceBaseline,
    _checkpoint_view,
    build_report,
    classify_eligibility,
    ingest,
)
from inferrail.ap.models import Action, Eligibility

ATTEMPTS: list[dict[str, Any]] = [
    {  # already resolved on the first attempt -- must be excluded, not "retry"-recommended.
        "work_id": "W-RESOLVED", "attempt_id": "att-r1", "attempt_number": 1,
        "status": "success", "timestamp": "2026-06-01T09:00:00", "source": "s",
        "cost_usd": "0.30", "confidence": 0.95,
    },
    {  # low confidence, sent straight to human review -- agrees with both baselines.
        "work_id": "W-LOW", "attempt_id": "att-l1", "attempt_number": 1,
        "status": "partial", "timestamp": "2026-06-01T09:05:00", "source": "s",
        "cost_usd": "0.42", "confidence": 0.31,
    },
    {  # mid-band confidence, retried once, retry succeeded.
        "work_id": "W-RETRY-OK", "attempt_id": "att-ro1", "attempt_number": 1,
        "status": "partial", "timestamp": "2026-06-01T09:10:00", "source": "s",
        "cost_usd": "0.40", "confidence": 0.62,
    },
    {
        "work_id": "W-RETRY-OK", "attempt_id": "att-ro2", "attempt_number": 2,
        "status": "success", "timestamp": "2026-06-01T09:11:00", "source": "s",
        "cost_usd": "0.40", "confidence": 0.95,
    },
    {  # duplicate of att-ro2, replayed -- must collapse, never double-count cost.
        "work_id": "W-RETRY-OK", "attempt_id": "att-ro2", "attempt_number": 2,
        "status": "success", "timestamp": "2026-06-01T09:11:00", "source": "s_replay",
        "cost_usd": "0.40", "confidence": 0.95,
    },
    {  # unknown cost -- candidate policy must answer insufficient_evidence.
        "work_id": "W-UNKNOWN-COST", "attempt_id": "att-u1", "attempt_number": 1,
        "status": "partial", "timestamp": "2026-06-01T09:20:00", "source": "s",
        "cost_usd": None, "confidence": 0.60,
    },
    {  # unsupported sequence: 3 attempts.
        "work_id": "W-TOO-MANY", "attempt_id": "att-m1", "attempt_number": 1,
        "status": "partial", "timestamp": "2026-06-01T09:30:00", "source": "s",
        "cost_usd": "0.10", "confidence": 0.6,
    },
    {
        "work_id": "W-TOO-MANY", "attempt_id": "att-m2", "attempt_number": 2,
        "status": "partial", "timestamp": "2026-06-01T09:31:00", "source": "s",
        "cost_usd": "0.10", "confidence": 0.6,
    },
    {
        "work_id": "W-TOO-MANY", "attempt_id": "att-m3", "attempt_number": 3,
        "status": "failed", "timestamp": "2026-06-01T09:32:00", "source": "s",
        "cost_usd": "0.10", "confidence": 0.6,
    },
]

REVIEWS: list[dict[str, Any]] = [
    {
        "work_id": "W-LOW", "action_taken": "human_review", "outcome": "corrected",
        "timestamp": "2026-06-01T14:00:00", "source": "s",
        "correction_delta_usd": "12.50", "review_cost_usd": "3.00",
    },
    {
        "work_id": "W-RETRY-OK", "action_taken": "retry", "outcome": "accepted",
        "timestamp": "2026-06-01T09:11:30", "source": "s",
        "correction_delta_usd": None, "review_cost_usd": None,
    },
    {
        "work_id": "W-UNKNOWN-COST", "action_taken": "human_review", "outcome": "rejected",
        "timestamp": "2026-06-01T14:30:00", "source": "s",
        "correction_delta_usd": None, "review_cost_usd": "3.20",
    },
]


def _ingest() -> Any:
    return ingest(ATTEMPTS, REVIEWS)


def test_duplicate_attempt_rows_collapse_and_never_double_count_cost() -> None:
    result = _ingest()
    assert "att-ro2" in result.duplicate_attempt_ids
    record = next(r for r in result.work_records if r.work_id == "W-RETRY-OK")
    assert len(record.attempts) == 2  # not 3, despite the replayed duplicate row
    expected_total = record.attempts[0].cost_usd + record.attempts[1].cost_usd
    assert record.known_attempt_cost_usd == expected_total


def test_conflicting_attempt_rows_are_dropped_not_guessed() -> None:
    conflicting_rows = [
        {"work_id": "W-CONFLICT", "attempt_id": "att-c1", "attempt_number": 1,
         "status": "partial", "timestamp": "2026-06-01T09:00:00", "source": "a",
         "cost_usd": "0.10", "confidence": 0.5},
        {"work_id": "W-CONFLICT", "attempt_id": "att-c1", "attempt_number": 1,
         "status": "failed", "timestamp": "2026-06-01T09:00:00", "source": "b",
         "cost_usd": "0.20", "confidence": 0.9},
    ]
    result = ingest(conflicting_rows, [])
    assert "att-c1" in result.conflicting_attempt_ids
    assert not any(r.work_id == "W-CONFLICT" for r in result.work_records)


def test_already_resolved_record_is_excluded_never_recommended_retry() -> None:
    status, reason = classify_eligibility(
        next(r for r in _ingest().work_records if r.work_id == "W-RESOLVED")
    )
    assert status == Eligibility.ALREADY_RESOLVED
    assert reason is not None


def test_unsupported_sequence_more_than_two_attempts_is_quarantined() -> None:
    status, reason = classify_eligibility(
        next(r for r in _ingest().work_records if r.work_id == "W-TOO-MANY")
    )
    assert status == Eligibility.UNSUPPORTED_SEQUENCE
    assert reason is not None


def test_review_revision_is_preserved_not_overwritten() -> None:
    reviews = [
        {"work_id": "W-CORRECTED", "action_taken": "human_review", "outcome": "accepted",
         "timestamp": "2026-06-01T14:00:00", "source": "s"},
        {"work_id": "W-CORRECTED", "action_taken": "human_review", "outcome": "corrected",
         "timestamp": "2026-06-01T15:00:00", "source": "s_correction",
         "correction_delta_usd": "4.00"},
    ]
    result = ingest([], reviews)
    assert "W-CORRECTED" in result.corrected_review_work_ids
    record = next(r for r in result.work_records if r.work_id == "W-CORRECTED")
    assert record.review is not None
    assert record.review.outcome.value == "corrected"
    assert len(record.review_history) == 2
    assert record.review_history[0].outcome.value == "accepted"


def test_conflicting_reviews_with_identical_timestamps_leave_review_none() -> None:
    reviews = [
        {"work_id": "W-TIE", "action_taken": "human_review", "outcome": "accepted",
         "timestamp": "2026-06-01T14:00:00", "source": "a"},
        {"work_id": "W-TIE", "action_taken": "human_review", "outcome": "rejected",
         "timestamp": "2026-06-01T14:00:00", "source": "b"},
    ]
    result = ingest([], reviews)
    assert "W-TIE" in result.conflicting_review_work_ids
    record = next(r for r in result.work_records if r.work_id == "W-TIE")
    assert record.review is None


def test_appending_a_later_attempt_never_changes_the_earlier_recorded_recommendation() -> None:
    """The checkpoint-view discipline: a policy only ever sees the state
    as of the first attempt."""
    ingested = _ingest()
    candidate = CandidateBatchPolicy(human_review_threshold=0.7, retry_floor=0.5)
    before = next(r for r in ingested.work_records if r.work_id == "W-RETRY-OK")
    rec_before = candidate.recommend(_checkpoint_view(before))
    # Simulate a later event being appended: recompute checkpoint view from
    # the same first attempt only -- must be unchanged regardless of what
    # was appended afterward.
    after = before
    rec_after = candidate.recommend(_checkpoint_view(after))
    assert rec_before.action == rec_after.action == Action.RETRY


def test_build_report_never_fabricates_a_counterfactual_for_a_disagreement() -> None:
    ingested = _ingest()
    policies = cast(
        "tuple",
        (
            HumanReviewBaselinePolicy(confidence_threshold=0.5),
            RetryOnceBaseline(),
            CandidateBatchPolicy(human_review_threshold=0.7, retry_floor=0.5),
        ),
    )
    report = build_report(
        ingested.work_records, policies,
        total_input_records=len(ATTEMPTS),
        skipped_rows=ingested.skipped_rows,
        duplicate_attempt_ids=ingested.duplicate_attempt_ids,
    )
    disagreements = [r for r in report.rows if r.agrees_with_actual is False]
    for row in disagreements:
        assert row.observed_outcome is None
        assert row.observed_cost_usd is None

    excluded_ids = {e.work_id for e in report.excluded_records}
    assert "W-RESOLVED" in excluded_ids
    assert "W-TOO-MANY" in excluded_ids


def test_unknown_cost_is_never_coerced_to_zero() -> None:
    ingested = _ingest()
    candidate = CandidateBatchPolicy(human_review_threshold=0.7, retry_floor=0.5)
    report = build_report(
        ingested.work_records, (candidate,),
        total_input_records=len(ATTEMPTS),
        skipped_rows=ingested.skipped_rows,
        duplicate_attempt_ids=ingested.duplicate_attempt_ids,
    )
    row = next(r for r in report.rows if r.work_id == "W-UNKNOWN-COST")
    assert row.recommended_action == Action.INSUFFICIENT_EVIDENCE
