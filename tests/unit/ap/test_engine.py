from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from inferrail.ap.adapters import FixtureRetryAdapter
from inferrail.ap.engine import RecoveryEngine
from inferrail.ap.handoff import LoggingHandoff
from inferrail.ap.models import (
    Action,
    AttemptStatus,
    CostEstimate,
    ExceptionCase,
    FailureType,
    RetryAttemptResult,
    ReviewOutcome,
)
from inferrail.ap.policy import PolicyConfig
from inferrail.ap.store import RecoveryStore
from inferrail.ap.validation import FieldPresenceAndConfidenceValidator

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _config(**overrides: object) -> PolicyConfig:
    defaults: dict[str, object] = {
        "eligible_failure_types": frozenset(
            {FailureType.LOW_CONFIDENCE.value, FailureType.VALIDATION_CHECK_FAILED.value}
        ),
        "retry_floor": 0.5,
        "human_review_threshold": 0.7,
        "max_retry_cost_usd": Decimal("1.00"),
        "decision_deadline_seconds": 3600.0,
    }
    defaults.update(overrides)
    return PolicyConfig(**defaults)  # type: ignore[arg-type]


def _engine(
    tmp_path: Path,
    *,
    fixture_results: dict[str, RetryAttemptResult] | None = None,
    config: PolicyConfig | None = None,
    min_confidence: float | None = 0.9,
) -> tuple[RecoveryEngine, RecoveryStore, LoggingHandoff]:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    handoff = LoggingHandoff(path=tmp_path / "handoffs.jsonl")
    fixture_results = fixture_results or {}
    engine = RecoveryEngine(
        store=store,
        config=config or _config(),
        retry_adapter=FixtureRetryAdapter(
            results_by_work_id=fixture_results,
            estimated_costs_by_work_id={
                work_id: Decimal("0.05") for work_id in fixture_results
            },
        ),
        validator=FieldPresenceAndConfidenceValidator(min_confidence=min_confidence),
        handoff=handoff,
    )
    return engine, store, handoff


def test_eligible_exception_successfully_recovered_by_one_retry(tmp_path: Path) -> None:
    engine, store, handoff = _engine(
        tmp_path,
        fixture_results={
            "WORK-1": RetryAttemptResult(
                attempt_id="ret-1", status=AttemptStatus.SUCCESS,
                cost_usd=Decimal("0.05"), confidence=0.97, provider="fixture",
            )
        },
    )
    case = ExceptionCase(
        work_id="WORK-1", checkpoint_attempt_id="ATT-1",
        failure_type=FailureType.LOW_CONFIDENCE.value, confidence=0.6,
        cost_so_far_usd=Decimal("0.10"), opened_at=NOW,
    )
    result = engine.decide(case, now=NOW)
    assert result.recommended_action == Action.RETRY
    assert result.status == "retry_resolved"
    assert result.retry_status == AttemptStatus.SUCCESS
    assert result.handoff_ref is None
    assert handoff.path.exists() is False


def test_unsuccessful_retry_falls_back_to_human_review(tmp_path: Path) -> None:
    engine, store, handoff = _engine(
        tmp_path,
        fixture_results={
            "WORK-2": RetryAttemptResult(
                attempt_id="ret-2", status=AttemptStatus.FAILED,
                cost_usd=Decimal("0.05"), confidence=0.40, provider="fixture",
            )
        },
    )
    case = ExceptionCase(
        work_id="WORK-2", checkpoint_attempt_id="ATT-2",
        failure_type=FailureType.LOW_CONFIDENCE.value, confidence=0.6,
        cost_so_far_usd=Decimal("0.10"), opened_at=NOW,
    )
    result = engine.decide(case, now=NOW)
    assert result.recommended_action == Action.RETRY
    assert result.status == "awaiting_human_review"
    assert result.retry_status == AttemptStatus.FAILED
    assert result.handoff_ref is not None
    assert handoff.path.exists()


def test_policy_disallows_retry_goes_straight_to_human_review_no_adapter_call(
    tmp_path: Path,
) -> None:
    engine, store, handoff = _engine(tmp_path, fixture_results={})
    case = ExceptionCase(
        work_id="WORK-3", checkpoint_attempt_id="ATT-3",
        failure_type=FailureType.LOW_CONFIDENCE.value, confidence=0.95,
        cost_so_far_usd=Decimal("0.10"), opened_at=NOW,
    )
    result = engine.decide(case, now=NOW)  # would KeyError if the adapter were called
    assert result.recommended_action == Action.HUMAN_REVIEW
    assert result.status == "awaiting_human_review"
    assert result.retry_status is None
    assert result.handoff_ref is not None


def test_repeated_request_is_idempotent_never_re_invokes_adapter_or_handoff(tmp_path: Path) -> None:
    calls: list[str] = []

    class CountingAdapter:
        name = "counting_adapter"

        def retry(self, case: ExceptionCase) -> RetryAttemptResult:
            calls.append(case.work_id)
            return RetryAttemptResult(
                attempt_id="ret-4", status=AttemptStatus.SUCCESS,
                cost_usd=Decimal("0.05"), confidence=0.97, provider="fixture",
            )

        def estimate_cost(self, case: ExceptionCase) -> CostEstimate:
            return CostEstimate(amount_usd=Decimal("0.05"), basis="test_declared")

    store = RecoveryStore(tmp_path / "ap.sqlite3")
    handoff = LoggingHandoff(path=tmp_path / "handoffs.jsonl")
    engine = RecoveryEngine(
        store=store, config=_config(), retry_adapter=CountingAdapter(),
        validator=FieldPresenceAndConfidenceValidator(min_confidence=0.9), handoff=handoff,
    )
    case = ExceptionCase(
        work_id="WORK-4", checkpoint_attempt_id="ATT-4",
        failure_type=FailureType.LOW_CONFIDENCE.value, confidence=0.6,
        cost_so_far_usd=Decimal("0.10"), opened_at=NOW,
    )
    first = engine.decide(case, now=NOW)
    second = engine.decide(case, now=NOW)
    third = engine.decide(case, now=NOW)

    assert calls == ["WORK-4"]  # the adapter was invoked exactly once
    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert third.idempotent_replay is True
    assert second.decision_id == first.decision_id == third.decision_id
    assert second.status == third.status == first.status


def test_ambiguous_retry_adapter_failure_never_retried_again_routes_to_human_review(
    tmp_path: Path,
) -> None:
    attempts = {"count": 0}

    class FlakyAdapter:
        name = "flaky_adapter"

        def retry(self, case: ExceptionCase) -> RetryAttemptResult:
            attempts["count"] += 1
            raise TimeoutError("simulated crash mid-call")

        def estimate_cost(self, case: ExceptionCase) -> CostEstimate:
            return CostEstimate(amount_usd=Decimal("0.05"), basis="test_declared")

    store = RecoveryStore(tmp_path / "ap.sqlite3")
    handoff = LoggingHandoff(path=tmp_path / "handoffs.jsonl")
    engine = RecoveryEngine(
        store=store, config=_config(), retry_adapter=FlakyAdapter(),
        validator=FieldPresenceAndConfidenceValidator(), handoff=handoff,
    )
    case = ExceptionCase(
        work_id="WORK-5", checkpoint_attempt_id="ATT-5",
        failure_type=FailureType.LOW_CONFIDENCE.value, confidence=0.6,
        cost_so_far_usd=Decimal("0.10"), opened_at=NOW,
    )
    result = engine.decide(case, now=NOW)
    assert result.retry_status == AttemptStatus.AMBIGUOUS
    assert result.status == "awaiting_human_review"
    assert result.handoff_ref is not None

    # Replaying must never call the flaky adapter again.
    replay = engine.decide(case, now=NOW)
    assert replay.idempotent_replay is True
    assert attempts["count"] == 1


def test_record_outcome_and_inspect_decision_and_outcome_records(tmp_path: Path) -> None:
    engine, store, _handoff = _engine(tmp_path, fixture_results={})
    case = ExceptionCase(
        work_id="WORK-6", checkpoint_attempt_id="ATT-6",
        failure_type=FailureType.LOW_CONFIDENCE.value, confidence=0.95,
        cost_so_far_usd=Decimal("0.10"), opened_at=NOW,
    )
    engine.decide(case, now=NOW)
    engine.record_outcome(
        work_id="WORK-6", outcome=ReviewOutcome.CORRECTED.value,
        timestamp=NOW.timestamp(), source="vendor_review_queue",
        correction_delta_usd=Decimal("12.50"), review_cost_usd=Decimal("3.00"),
    )
    decision = store.get_decision("WORK-6")
    assert decision is not None
    assert decision["status"] == "resolved"
    history = store.get_outcome_history("WORK-6")
    assert len(history) == 1
    assert history[0]["outcome"] == ReviewOutcome.CORRECTED.value

    # A delayed correction is preserved, not overwritten.
    engine.record_outcome(
        work_id="WORK-6", outcome=ReviewOutcome.REJECTED.value,
        timestamp=NOW.timestamp() + 100, source="vendor_review_queue_correction",
    )
    history = store.get_outcome_history("WORK-6")
    assert len(history) == 2
    assert history[0]["outcome"] == ReviewOutcome.CORRECTED.value
    assert history[-1]["outcome"] == ReviewOutcome.REJECTED.value


def test_already_resolved_work_id_cannot_record_outcome_without_a_decision(tmp_path: Path) -> None:
    engine, _store, _handoff = _engine(tmp_path, fixture_results={})
    with pytest.raises(KeyError):
        engine.record_outcome(
            work_id="NEVER-DECIDED", outcome=ReviewOutcome.ACCEPTED.value, timestamp=NOW.timestamp()
        )


# --- Defect #3: prospective retry cost authorization -----------------------


def test_execute_retry_never_invoked_when_estimate_exceeds_max_retry_cost_usd(
    tmp_path: Path,
) -> None:
    """Reproduced defect: max_retry_cost_usd=$1, prior spend $0.10, and a
    fixture adapter reporting a $10 retry was still invoked and accepted.
    Now: the adapter must never be called at all."""
    calls: list[str] = []

    class SpyAdapter:
        name = "spy_adapter"

        def retry(self, case: ExceptionCase) -> RetryAttemptResult:
            calls.append(case.work_id)
            return RetryAttemptResult(
                attempt_id="ret-x", status=AttemptStatus.SUCCESS,
                cost_usd=Decimal("10.00"), confidence=0.99, provider="fixture",
            )

        def estimate_cost(self, case: ExceptionCase) -> CostEstimate:
            return CostEstimate(amount_usd=Decimal("10.00"), basis="test_declared")

    store = RecoveryStore(tmp_path / "ap.sqlite3")
    handoff = LoggingHandoff(path=tmp_path / "handoffs.jsonl")
    engine = RecoveryEngine(
        store=store, config=_config(max_retry_cost_usd=Decimal("1.00")),
        retry_adapter=SpyAdapter(),
        validator=FieldPresenceAndConfidenceValidator(), handoff=handoff,
    )
    case = ExceptionCase(
        work_id="WORK-COST-1", checkpoint_attempt_id="ATT",
        failure_type=FailureType.LOW_CONFIDENCE.value,
        confidence=0.6, cost_so_far_usd=Decimal("0.10"), opened_at=NOW,
    )
    result = engine.decide(case, now=NOW)

    assert calls == []  # never invoked
    assert result.status == "awaiting_human_review"
    assert result.handoff_ref is not None
    assert store.get_retry_attempt("WORK-COST-1") is None  # no retry_attempts row at all

    # Repeated decide() never invokes the adapter either.
    engine.decide(case, now=NOW)
    assert calls == []


def test_execute_retry_never_invoked_when_estimate_unknown_routes_to_review(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class NoEstimateAdapter:
        name = "no_estimate_adapter"

        def retry(self, case: ExceptionCase) -> RetryAttemptResult:
            calls.append(case.work_id)
            return RetryAttemptResult(
                attempt_id="ret-y", status=AttemptStatus.SUCCESS,
                cost_usd=Decimal("0.05"), confidence=0.99, provider="fixture",
            )

    store = RecoveryStore(tmp_path / "ap.sqlite3")
    handoff = LoggingHandoff(path=tmp_path / "handoffs.jsonl")
    engine = RecoveryEngine(
        store=store, config=_config(), retry_adapter=NoEstimateAdapter(),
        validator=FieldPresenceAndConfidenceValidator(), handoff=handoff,
    )
    case = ExceptionCase(
        work_id="WORK-COST-2", checkpoint_attempt_id="ATT",
        failure_type=FailureType.LOW_CONFIDENCE.value,
        confidence=0.6, cost_so_far_usd=Decimal("0.10"), opened_at=NOW,
    )
    result = engine.decide(case, now=NOW)
    assert calls == []
    assert result.status == "awaiting_human_review"


def test_honest_overrun_recorded_when_actual_exceeds_estimate(tmp_path: Path) -> None:
    class UnderestimatingAdapter:
        name = "underestimating_adapter"

        def retry(self, case: ExceptionCase) -> RetryAttemptResult:
            return RetryAttemptResult(
                attempt_id="ret-z", status=AttemptStatus.SUCCESS,
                cost_usd=Decimal("0.20"), confidence=0.99, provider="fixture",
            )

        def estimate_cost(self, case: ExceptionCase) -> CostEstimate:
            return CostEstimate(amount_usd=Decimal("0.05"), basis="test_declared")

    store = RecoveryStore(tmp_path / "ap.sqlite3")
    handoff = LoggingHandoff(path=tmp_path / "handoffs.jsonl")
    engine = RecoveryEngine(
        store=store, config=_config(), retry_adapter=UnderestimatingAdapter(),
        validator=FieldPresenceAndConfidenceValidator(min_confidence=0.9), handoff=handoff,
    )
    case = ExceptionCase(
        work_id="WORK-COST-3", checkpoint_attempt_id="ATT",
        failure_type=FailureType.LOW_CONFIDENCE.value,
        confidence=0.6, cost_so_far_usd=Decimal("0.10"), opened_at=NOW,
    )
    engine.decide(case, now=NOW)

    from inferrail.ap.report import build_live_report

    row = build_live_report(store).rows[0]
    assert row.pre_flight_estimate_usd == Decimal("0.05")
    assert row.retry_cost_overrun_usd == Decimal("0.15")


# --- Defect #2: crash recovery (lease lifecycle) ----------------------------


def test_lease_is_set_on_retry_in_progress_and_cleared_on_terminal_status(
    tmp_path: Path,
) -> None:
    engine, store, _handoff = _engine(
        tmp_path,
        fixture_results={
            "WORK-LEASE-1": RetryAttemptResult(
                attempt_id="ret-1", status=AttemptStatus.SUCCESS,
                cost_usd=Decimal("0.05"), confidence=0.97, provider="fixture",
            )
        },
    )
    case = ExceptionCase(
        work_id="WORK-LEASE-1", checkpoint_attempt_id="ATT",
        failure_type=FailureType.LOW_CONFIDENCE.value,
        confidence=0.6, cost_so_far_usd=Decimal("0.10"), opened_at=NOW,
    )
    engine.decide(case, now=NOW)
    decision = store.get_decision("WORK-LEASE-1")
    assert decision["status"] == "retry_resolved"
    assert decision["worker_id"] is None
    assert decision["lease_expires_at"] is None


def test_late_retry_result_after_reap_is_recorded_not_silently_dropped(tmp_path: Path) -> None:
    """Simulates the exact review-reported scenario: a decision's lease
    expires and gets reaped, but the "dead" worker was actually still
    running and its real result arrives afterward -- it must never be
    silently lost, and the decision must never be flipped back to
    retry_resolved without a receiving system acknowledging anything."""
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    handoff = LoggingHandoff(path=tmp_path / "handoffs.jsonl")

    class SlowAdapter:
        name = "slow_adapter"

        def retry(self, case: ExceptionCase) -> RetryAttemptResult:
            return RetryAttemptResult(
                attempt_id="ret-slow", status=AttemptStatus.SUCCESS,
                cost_usd=Decimal("0.06"), confidence=0.97, provider="fixture",
            )

        def estimate_cost(self, case: ExceptionCase) -> CostEstimate:
            return CostEstimate(amount_usd=Decimal("0.06"), basis="test_declared")

    engine = RecoveryEngine(
        store=store, config=_config(), retry_adapter=SlowAdapter(),
        validator=FieldPresenceAndConfidenceValidator(min_confidence=0.9), handoff=handoff,
        retry_lease_seconds=0.01,
    )
    case = ExceptionCase(
        work_id="WORK-LATE", checkpoint_attempt_id="ATT",
        failure_type=FailureType.LOW_CONFIDENCE.value,
        confidence=0.6, cost_so_far_usd=Decimal("0.10"), opened_at=NOW,
    )

    # Simulate: the decision was created (lease set), but this "worker"
    # is slow -- meanwhile its lease expires and gets reaped by another
    # process/operator.
    store.create_decision(
        work_id="WORK-LATE", decision_id="dec-late", checkpoint_attempt_id="ATT",
        failure_type=FailureType.LOW_CONFIDENCE.value, confidence="0.6", cost_so_far_usd="0.10",
        policy_name="candidate_policy", policy_version="ap.policy/v1",
        recommended_action="retry", reason="test", status="retry_in_progress",
        worker_id="dead-worker", lease_expires_at=100.0,
    )
    reaped = store.reap_stale_retry_lease("WORK-LATE", now=200.0)
    assert reaped is not None

    # Now the "slow" adapter call actually completes -- record_retry_attempt
    # hits AmbiguousRetryError since a synthetic attempt already exists.
    result = engine._execute_retry(
        case, "dec-late",
        recommendation=type(
            "R", (), {"reason": "test", "policy_version": "ap.policy/v1", "action": Action.RETRY}
        )(),
    )
    assert result.late_result_recorded is True
    assert result.status == "awaiting_human_review"  # never silently flipped to retry_resolved

    late = store.get_late_retry_results("WORK-LATE")
    assert len(late) == 1
    assert late[0]["cost_usd"] == "0.06"  # real cost is preserved, never lost


def test_reap_stale_retries_sweeps_and_returns_reaped_work_ids(tmp_path: Path) -> None:
    engine, store, _handoff = _engine(tmp_path, fixture_results={})
    store.create_decision(
        work_id="SWEEP-1", decision_id="dec-sweep-1", checkpoint_attempt_id="ATT",
        failure_type="low_confidence", confidence="0.6", cost_so_far_usd="0.10",
        policy_name="candidate_policy", policy_version="ap.policy/v1",
        recommended_action="retry", reason="test", status="retry_in_progress",
        worker_id="dead-worker", lease_expires_at=100.0,
    )
    reaped = engine.reap_stale_retries(now=datetime.fromtimestamp(200.0, tz=UTC))
    assert len(reaped) == 1
    assert reaped[0]["work_id"] == "SWEEP-1"

    # Idempotent: a repeat sweep reaps nothing new.
    assert engine.reap_stale_retries(now=datetime.fromtimestamp(200.0, tz=UTC)) == []


def test_ensure_handoff_is_idempotent_and_rejects_resolved_decisions(tmp_path: Path) -> None:
    engine, store, handoff = _engine(tmp_path, fixture_results={})
    case = ExceptionCase(
        work_id="WORK-ENSURE", checkpoint_attempt_id="ATT",
        failure_type=FailureType.LOW_CONFIDENCE.value,
        confidence=0.95, cost_so_far_usd=Decimal("0.10"), opened_at=NOW,
    )
    result = engine.decide(case, now=NOW)
    assert result.handoff_ref is not None

    # Calling ensure_handoff again for an already-handed-off work_id
    # returns the same ref without re-sending.
    ref = engine.ensure_handoff(case)
    assert ref == result.handoff_ref

    with pytest.raises(ValueError):
        engine.ensure_handoff(
            ExceptionCase(
                work_id="NEVER-DECIDED", checkpoint_attempt_id="X", failure_type="low_confidence"
            )
        )


def test_handoff_send_failure_raises_handoff_send_failed_and_is_retriable(tmp_path: Path) -> None:
    from inferrail.ap.handoff import HandoffSendFailed

    class FlakyHandoff:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, case, recommendation, attempt_history):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("review queue unreachable")
            return "queue-ticket-recovered"

    store = RecoveryStore(tmp_path / "ap.sqlite3")
    flaky = FlakyHandoff()
    engine = RecoveryEngine(
        store=store, config=_config(), retry_adapter=FixtureRetryAdapter(results_by_work_id={}),
        validator=FieldPresenceAndConfidenceValidator(), handoff=flaky,
    )
    case = ExceptionCase(
        work_id="WORK-FLAKY-HANDOFF", checkpoint_attempt_id="ATT",
        failure_type=FailureType.LOW_CONFIDENCE.value, confidence=0.95,
        cost_so_far_usd=Decimal("0.10"), opened_at=NOW,
    )
    with pytest.raises(HandoffSendFailed):
        engine.decide(case, now=NOW)

    # No handoff row was recorded -- retrying via ensure_handoff succeeds.
    assert store.get_handoff("WORK-FLAKY-HANDOFF") is None
    ref = engine.ensure_handoff(case)
    assert ref == "queue-ticket-recovered"
