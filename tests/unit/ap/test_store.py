from __future__ import annotations

from pathlib import Path

import pytest

from inferrail.ap.store import AmbiguousRetryError, RecoveryStore


def test_create_decision_is_idempotent_on_work_id(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    row1, created1 = store.create_decision(
        work_id="W1", decision_id="dec-1", checkpoint_attempt_id="A1",
        failure_type="low_confidence", confidence="0.6", cost_so_far_usd="0.10",
        policy_name="candidate_policy", policy_version="ap.policy/v1",
        recommended_action="retry", reason="test", status="retry_in_progress",
    )
    row2, created2 = store.create_decision(
        work_id="W1", decision_id="dec-2", checkpoint_attempt_id="A1",
        failure_type="low_confidence", confidence="0.6", cost_so_far_usd="0.10",
        policy_name="candidate_policy", policy_version="ap.policy/v1",
        recommended_action="human_review", reason="different", status="awaiting_human_review",
    )
    assert created1 is True
    assert created2 is False
    assert row2["decision_id"] == "dec-1"  # the second call never overwrote the first
    assert row2["recommended_action"] == "retry"


def test_second_distinct_retry_attempt_for_same_work_id_raises(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    store.create_decision(
        work_id="W2", decision_id="dec-1", checkpoint_attempt_id="A1",
        failure_type="low_confidence", confidence="0.6", cost_so_far_usd=None,
        policy_name="p", policy_version="v1", recommended_action="retry",
        reason="r", status="retry_in_progress",
    )
    store.record_retry_attempt(
        work_id="W2", attempt_id="ret-1", status="success",
        cost_usd="0.05", confidence="0.9", provider="fixture",
    )
    with pytest.raises(AmbiguousRetryError):
        store.record_retry_attempt(
            work_id="W2", attempt_id="ret-2", status="success",
            cost_usd="0.05", confidence="0.9", provider="fixture",
        )


def test_recording_the_same_attempt_id_twice_is_idempotent(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    store.create_decision(
        work_id="W3", decision_id="dec-1", checkpoint_attempt_id="A1",
        failure_type="low_confidence", confidence="0.6", cost_so_far_usd=None,
        policy_name="p", policy_version="v1", recommended_action="retry",
        reason="r", status="retry_in_progress",
    )
    row1, created1 = store.record_retry_attempt(
        work_id="W3", attempt_id="ret-1", status="success",
        cost_usd="0.05", confidence="0.9", provider="fixture",
    )
    row2, created2 = store.record_retry_attempt(
        work_id="W3", attempt_id="ret-1", status="success",
        cost_usd="0.05", confidence="0.9", provider="fixture",
    )
    assert created1 is True
    assert created2 is False
    assert row1 == row2


def test_handoff_is_idempotent_on_work_id(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    store.create_decision(
        work_id="W4", decision_id="dec-1", checkpoint_attempt_id="A1",
        failure_type="low_confidence", confidence=None, cost_so_far_usd=None,
        policy_name="p", policy_version="v1", recommended_action="human_review",
        reason="r", status="awaiting_human_review",
    )
    row1, created1 = store.record_handoff(work_id="W4", handoff_ref="ref-1")
    row2, created2 = store.record_handoff(work_id="W4", handoff_ref="ref-2")
    assert created1 is True
    assert created2 is False
    assert row2["handoff_ref"] == "ref-1"


def test_outcome_history_is_append_only_and_ordered(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    store.create_decision(
        work_id="W5", decision_id="dec-1", checkpoint_attempt_id="A1",
        failure_type="low_confidence", confidence=None, cost_so_far_usd=None,
        policy_name="p", policy_version="v1", recommended_action="human_review",
        reason="r", status="awaiting_human_review",
    )
    store.record_outcome(
        work_id="W5", outcome="accepted", timestamp=1.0, source="s",
        correction_delta_usd=None, review_cost_usd="1.00",
    )
    store.record_outcome(
        work_id="W5", outcome="corrected", timestamp=2.0, source="s",
        correction_delta_usd="5.00", review_cost_usd="1.00",
    )
    history = store.get_outcome_history("W5")
    assert [h["outcome"] for h in history] == ["accepted", "corrected"]


def test_delete_work_id_removes_every_related_record(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    store.create_decision(
        work_id="W6", decision_id="dec-1", checkpoint_attempt_id="A1",
        failure_type="low_confidence", confidence="0.6", cost_so_far_usd=None,
        policy_name="p", policy_version="v1", recommended_action="retry",
        reason="r", status="retry_in_progress",
    )
    store.record_retry_attempt(
        work_id="W6", attempt_id="ret-1", status="success",
        cost_usd="0.05", confidence="0.9", provider="fixture",
    )
    store.record_handoff(work_id="W6", handoff_ref="ref-1")
    store.record_outcome(
        work_id="W6", outcome="accepted", timestamp=1.0, source="s",
        correction_delta_usd=None, review_cost_usd=None,
    )

    deleted = store.delete_work_id("W6")
    assert deleted is True
    assert store.get_decision("W6") is None
    assert store.get_retry_attempt("W6") is None
    assert store.get_handoff("W6") is None
    assert store.get_outcome_history("W6") == []


def test_delete_work_id_returns_false_when_nothing_to_delete(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    assert store.delete_work_id("NEVER-EXISTED") is False


def test_record_outcome_without_a_decision_raises_keyerror(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    with pytest.raises(KeyError):
        store.record_outcome(
            work_id="NEVER", outcome="accepted", timestamp=1.0, source="s",
            correction_delta_usd=None, review_cost_usd=None,
        )


def _leased_decision(store: RecoveryStore, work_id: str, *, lease_expires_at: float) -> None:
    store.create_decision(
        work_id=work_id, decision_id=f"dec-{work_id}", checkpoint_attempt_id="A1",
        failure_type="low_confidence", confidence="0.6", cost_so_far_usd="0.10",
        policy_name="candidate_policy", policy_version="ap.policy/v1",
        recommended_action="retry", reason="test", status="retry_in_progress",
        worker_id="dead-worker", lease_expires_at=lease_expires_at,
    )


def test_reap_stale_retry_lease_transitions_to_awaiting_human_review(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _leased_decision(store, "W7", lease_expires_at=100.0)

    result = store.reap_stale_retry_lease("W7", now=200.0)
    assert result is not None
    assert result["status"] == "ambiguous"
    assert result["provider"] == "lease_reaper"

    decision = store.get_decision("W7")
    assert decision is not None
    assert decision["status"] == "awaiting_human_review"
    assert decision["worker_id"] is None
    assert decision["lease_expires_at"] is None


def test_reap_does_nothing_if_lease_not_yet_stale(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _leased_decision(store, "W8", lease_expires_at=500.0)

    result = store.reap_stale_retry_lease("W8", now=200.0)
    assert result is None
    assert store.get_decision("W8")["status"] == "retry_in_progress"


def test_reap_is_idempotent_on_repeat_calls(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _leased_decision(store, "W9", lease_expires_at=100.0)

    first = store.reap_stale_retry_lease("W9", now=200.0)
    assert first is not None
    second = store.reap_stale_retry_lease("W9", now=200.0)
    assert second is None  # already reaped -- no-op, not a duplicate attempt row

    attempts = store.get_retry_attempt("W9")
    assert attempts is not None and attempts["attempt_id"] == "ret_reaped_W9"


def test_reap_does_nothing_if_a_real_attempt_already_exists(tmp_path: Path) -> None:
    """The race where the "dead" worker actually finished between the
    staleness scan and the reap call -- the real result must never be
    overwritten or duplicated."""
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _leased_decision(store, "W10", lease_expires_at=100.0)
    store.record_retry_attempt(
        work_id="W10", attempt_id="ret-real", status="success",
        cost_usd="0.06", confidence="0.9", provider="fixture",
    )

    result = store.reap_stale_retry_lease("W10", now=200.0)
    assert result is None
    attempts = store.get_retry_attempt("W10")
    assert attempts["attempt_id"] == "ret-real"


def test_find_stale_retry_leases_only_returns_expired_ones(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _leased_decision(store, "STALE", lease_expires_at=100.0)
    _leased_decision(store, "FRESH", lease_expires_at=9999.0)

    stale = store.find_stale_retry_leases(now=200.0)
    assert [row["work_id"] for row in stale] == ["STALE"]


def test_clear_retry_lease_removes_worker_and_expiry(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _leased_decision(store, "W11", lease_expires_at=9999.0)
    store.clear_retry_lease("W11")
    decision = store.get_decision("W11")
    assert decision["worker_id"] is None
    assert decision["lease_expires_at"] is None


def test_record_and_get_late_retry_results(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _leased_decision(store, "W12", lease_expires_at=100.0)
    store.reap_stale_retry_lease("W12", now=200.0)

    store.record_late_retry_result(
        work_id="W12", attempt_id="ret-late", status="success",
        cost_usd="0.06", provider="fixture", detail="arrived late",
    )
    late = store.get_late_retry_results("W12")
    assert len(late) == 1
    assert late[0]["attempt_id"] == "ret-late"
    assert late[0]["status"] == "success"
