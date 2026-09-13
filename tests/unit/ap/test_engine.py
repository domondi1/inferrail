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
    engine = RecoveryEngine(
        store=store,
        config=config or _config(),
        retry_adapter=FixtureRetryAdapter(results_by_work_id=fixture_results or {}),
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
