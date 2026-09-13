"""Regression tests for the reproduced cost-completeness defect in
`report.build_live_report`/`_observed_cost`: a retry that reports
`status="success"` but fails *validation* used to be marked complete
anyway, and a retry-then-review case with an unknown review cost was
also wrongly marked complete because the missing-review-cost check
explicitly excluded `recommended_action == "retry"`. See
`report._observed_cost`'s docstring for the fix."""

from __future__ import annotations

from pathlib import Path

from inferrail.ap.report import build_live_report
from inferrail.ap.store import RecoveryStore


def _decision(store: RecoveryStore, work_id: str, *, status: str = "retry_in_progress") -> None:
    store.create_decision(
        work_id=work_id, decision_id=f"dec-{work_id}", checkpoint_attempt_id=f"att-{work_id}",
        failure_type="low_confidence", confidence="0.6", cost_so_far_usd="0.10",
        policy_name="candidate_policy", policy_version="ap.policy/v1",
        recommended_action="retry", reason="test", status=status,
    )


def test_validation_failure_after_successful_retry_marks_incomplete_until_resolved(
    tmp_path: Path,
) -> None:
    """The exact reproduced scenario: a retry reports status="success"
    with a known $0.06 cost, but fails validation and falls to human
    review. The report must not claim the observed cost is complete
    until a review outcome (with a known cost) actually resolves it."""
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W1")
    store.record_retry_attempt(
        work_id="W1", attempt_id="ret-1", status="success", cost_usd="0.06",
        confidence="0.4", provider="fixture", validation_passed="False",
        validator_version="ap.validator/v1",
    )
    store.set_decision_status("W1", "awaiting_human_review")

    row = build_live_report(store).rows[0]
    assert row.retry_status == "success"
    assert row.validation_passed is False
    assert row.observed_cost_complete is False, (
        "a retry that reported success but failed validation must not be "
        "marked cost-complete while still awaiting review"
    )

    # A review outcome with an unknown review cost still leaves it incomplete.
    store.record_outcome(
        work_id="W1", outcome="corrected", timestamp=0.0, source="vendor_review_queue",
        correction_delta_usd=None, review_cost_usd=None,
    )
    row = build_live_report(store).rows[0]
    assert row.status == "resolved"
    assert row.observed_cost_complete is False, (
        "a recorded review outcome with an unknown review cost must not be "
        "marked cost-complete, even though the decision itself is now resolved"
    )

    # Only once the review cost is actually known does it become complete.
    store.record_outcome(
        work_id="W1", outcome="corrected", timestamp=1.0, source="vendor_review_queue",
        correction_delta_usd="9.40", review_cost_usd="2.50",
    )
    row = build_live_report(store).rows[0]
    assert row.observed_cost_complete is True
    assert row.observed_cost_usd == row.retry_cost_usd + row.review_cost_usd
    assert row.outcome_revision_count == 2


def test_pending_review_with_no_outcome_yet_is_incomplete(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W2", status="awaiting_human_review")
    row = build_live_report(store).rows[0]
    assert row.observed_cost_complete is False
    assert row.observed_cost_usd is None


def test_recorded_review_with_missing_cost_stays_incomplete_even_for_retry_action(
    tmp_path: Path,
) -> None:
    """Previously the missing-review-cost check explicitly excluded
    `recommended_action == "retry"`, so this exact shape (a retry
    decision, review recorded, review cost unknown) was wrongly marked
    complete."""
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W3")
    store.record_retry_attempt(
        work_id="W3", attempt_id="ret-1", status="failed", cost_usd="0.06",
        confidence="0.2", provider="fixture", validation_passed="False",
        validator_version="ap.validator/v1",
    )
    store.set_decision_status("W3", "awaiting_human_review")
    store.record_outcome(
        work_id="W3", outcome="rejected", timestamp=0.0, source="vendor_review_queue",
        correction_delta_usd=None, review_cost_usd=None,
    )
    row = build_live_report(store).rows[0]
    assert row.recommended_action == "retry"
    assert row.observed_cost_complete is False


def test_failed_retry_marks_incomplete_until_review_resolves(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W4")
    store.record_retry_attempt(
        work_id="W4", attempt_id="ret-1", status="failed", cost_usd="0.06",
        confidence="0.2", provider="fixture", validation_passed="False",
        validator_version="ap.validator/v1",
    )
    store.set_decision_status("W4", "awaiting_human_review")
    row = build_live_report(store).rows[0]
    assert row.observed_cost_complete is False


def test_ambiguous_retry_marks_incomplete(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W5")
    store.record_retry_attempt(
        work_id="W5", attempt_id="ret-1", status="ambiguous", cost_usd=None,
        confidence=None, provider="fixture",
    )
    store.set_decision_status("W5", "awaiting_human_review")
    row = build_live_report(store).rows[0]
    assert row.observed_cost_usd is None
    assert row.observed_cost_complete is False


def test_outcome_revision_recomputes_completeness_off_latest_only(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W6")
    store.record_retry_attempt(
        work_id="W6", attempt_id="ret-1", status="success", cost_usd="0.06",
        confidence="0.4", provider="fixture", validation_passed="False",
        validator_version="ap.validator/v1",
    )
    store.set_decision_status("W6", "awaiting_human_review")
    store.record_outcome(
        work_id="W6", outcome="corrected", timestamp=0.0, source="vendor_review_queue",
        correction_delta_usd="5.00", review_cost_usd="1.00",
    )
    row = build_live_report(store).rows[0]
    assert row.observed_cost_complete is True
    assert row.outcome_revision_count == 1

    # A later correction with an unknown cost supersedes the prior one --
    # completeness reflects only the latest outcome, not the earlier known one.
    store.record_outcome(
        work_id="W6", outcome="rejected", timestamp=1.0, source="vendor_review_queue_correction",
        correction_delta_usd=None, review_cost_usd=None,
    )
    row = build_live_report(store).rows[0]
    assert row.outcome_revision_count == 2
    assert row.established_outcome == "rejected"
    assert row.observed_cost_complete is False


def test_retry_cost_overrun_recorded_honestly(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W7")
    store.record_retry_attempt(
        work_id="W7", attempt_id="ret-1", status="success", cost_usd="0.20",
        confidence="0.9", provider="fixture", validation_passed="True",
        validator_version="ap.validator/v1", pre_flight_estimate_usd="0.05",
    )
    store.set_decision_status("W7", "retry_resolved")
    row = build_live_report(store).rows[0]
    assert row.pre_flight_estimate_usd is not None
    from decimal import Decimal

    assert row.retry_cost_overrun_usd == Decimal("0.15")


def test_late_retry_result_surfaced_exactly_once_never_promoted_to_accepted(
    tmp_path: Path,
) -> None:
    """A real result that arrives after this work_id's lease was
    already reaped must be preserved and surfaced for audit -- but
    never silently promoted to an accepted business outcome: the
    decision's authoritative status and observed_cost_usd/
    observed_cost_complete must be unaffected by it."""
    from decimal import Decimal

    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W8")
    # Simulate a reap: a synthetic ambiguous attempt was recorded and
    # the decision moved to awaiting_human_review.
    store.record_retry_attempt(
        work_id="W8", attempt_id="ret_reaped_W8", status="ambiguous",
        cost_usd=None, confidence=None, provider="lease_reaper",
    )
    store.set_decision_status("W8", "awaiting_human_review")

    row = build_live_report(store).rows[0]
    assert row.late_result_status is None
    assert row.late_result_cost_usd is None

    # Now the "dead" worker's real result arrives late.
    store.record_late_retry_result(
        work_id="W8", attempt_id="ret-real-late", status="success",
        cost_usd="0.09", provider="fixture", detail="arrived after reap",
    )

    row = build_live_report(store).rows[0]
    assert row.late_result_status == "success"
    assert row.late_result_cost_usd == Decimal("0.09")
    # Never promoted: the official record stays exactly as the reap left it.
    assert row.status == "awaiting_human_review"
    assert row.retry_status == "ambiguous"
    assert row.observed_cost_usd is None
    assert row.observed_cost_complete is False


def test_late_retry_result_surfaces_only_the_latest_when_more_than_one(
    tmp_path: Path,
) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W9")
    store.record_retry_attempt(
        work_id="W9", attempt_id="ret_reaped_W9", status="ambiguous",
        cost_usd=None, confidence=None, provider="lease_reaper",
    )
    store.set_decision_status("W9", "awaiting_human_review")
    store.record_late_retry_result(
        work_id="W9", attempt_id="ret-late-1", status="failed",
        cost_usd="0.05", provider="fixture", detail="first late arrival",
    )
    store.record_late_retry_result(
        work_id="W9", attempt_id="ret-late-2", status="success",
        cost_usd="0.09", provider="fixture", detail="second late arrival",
    )

    row = build_live_report(store).rows[0]
    assert row.late_result_status == "success"  # only the latest, exactly once
