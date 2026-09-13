from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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


def test_reap_reconciles_to_retry_resolved_when_a_real_validated_attempt_already_exists(
    tmp_path: Path,
) -> None:
    """The crash boundary between `record_retry_attempt` succeeding and
    the decision's own status transition: the real attempt is already
    durably recorded (validation_passed="True"), but the process died
    before `set_decision_status`/`clear_retry_lease` ran. Reaping must
    never insert a second, synthetic attempt, and must never leave the
    decision stuck at retry_in_progress forever -- it reconciles using
    the real attempt's own recorded validation result."""
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _leased_decision(store, "W10", lease_expires_at=100.0)
    store.record_retry_attempt(
        work_id="W10", attempt_id="ret-real", status="success",
        cost_usd="0.06", confidence="0.9", provider="fixture",
        validation_passed="True", validator_version="ap.validator/v1",
    )

    result = store.reap_stale_retry_lease("W10", now=200.0)
    assert result is not None
    assert result["kind"] == "reconciled"
    assert result["attempt_id"] == "ret-real"  # never a second, synthetic attempt

    decision = store.get_decision("W10")
    assert decision["status"] == "retry_resolved"
    assert decision["worker_id"] is None
    assert decision["lease_expires_at"] is None
    attempts = store.get_retry_attempt("W10")
    assert attempts["attempt_id"] == "ret-real"
    assert attempts["cost_usd"] == "0.06"  # the real, known cost is preserved exactly


def test_reap_reconciles_to_awaiting_human_review_when_validation_failed(
    tmp_path: Path,
) -> None:
    """Same crash boundary, but the real attempt's own validation did
    not pass -- reconciliation must route to human review, never
    retry_resolved, exactly matching what `_execute_retry` itself would
    have done had it not crashed."""
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _leased_decision(store, "W11", lease_expires_at=100.0)
    store.record_retry_attempt(
        work_id="W11", attempt_id="ret-real-2", status="success",
        cost_usd="0.06", confidence="0.4", provider="fixture",
        validation_passed="False", validator_version="ap.validator/v1",
    )

    result = store.reap_stale_retry_lease("W11", now=200.0)
    assert result["kind"] == "reconciled"
    decision = store.get_decision("W11")
    assert decision["status"] == "awaiting_human_review"


def test_reap_reconcile_is_idempotent_on_repeat_calls(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _leased_decision(store, "W12b", lease_expires_at=100.0)
    store.record_retry_attempt(
        work_id="W12b", attempt_id="ret-real-3", status="success",
        cost_usd="0.06", confidence="0.9", provider="fixture",
        validation_passed="True", validator_version="ap.validator/v1",
    )
    first = store.reap_stale_retry_lease("W12b", now=200.0)
    assert first["kind"] == "reconciled"
    second = store.reap_stale_retry_lease("W12b", now=200.0)
    assert second is None  # already resolved -- no-op, not re-reconciled


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


def test_concurrent_create_decision_for_the_same_work_id_serializes_to_one_winner(
    tmp_path: Path,
) -> None:
    """Two real OS threads race to create a decision for the same
    work_id. `BEGIN IMMEDIATE` (plus `PRAGMA busy_timeout`) must
    serialize them via real SQLite file-locking -- exactly one call
    creates the row, the other sees it already exists -- rather than
    both racing an in-memory check. Exercised on every supported
    platform's own SQLite/filesystem locking semantics, not just POSIX's
    (see `.github/workflows/platform-verify.yml`)."""
    db_path = tmp_path / "ap.sqlite3"

    def try_create(decision_id: str) -> bool:
        # Each thread uses its own RecoveryStore (and therefore its own
        # SQLite connection) against the same db file, matching how
        # independent concurrent callers would behave.
        _row, created = RecoveryStore(db_path).create_decision(
            work_id="RACE-1", decision_id=decision_id, checkpoint_attempt_id="A1",
            failure_type="low_confidence", confidence="0.6", cost_so_far_usd="0.10",
            policy_name="candidate_policy", policy_version="ap.policy/v1",
            recommended_action="retry", reason="test", status="retry_in_progress",
        )
        return created

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(try_create, ["dec-a", "dec-b"]))

    assert sorted(results) == [False, True]  # exactly one winner, never both, never neither

    store = RecoveryStore(db_path)
    decision = store.get_decision("RACE-1")
    assert decision is not None
    assert decision["decision_id"] in ("dec-a", "dec-b")  # whichever won, only one row exists


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
