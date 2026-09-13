from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from inferrail.ap.models import Action, CostEstimate, ExceptionCase, FailureType
from inferrail.ap.policy import CONFIG_VERSION, PolicyConfig, authorize_retry_cost, recommend

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


def _case(**overrides: object) -> ExceptionCase:
    defaults: dict[str, object] = {
        "work_id": "WORK-1",
        "checkpoint_attempt_id": "ATT-1",
        "failure_type": FailureType.LOW_CONFIDENCE.value,
        "confidence": 0.6,
        "cost_so_far_usd": Decimal("0.10"),
        "opened_at": NOW,
    }
    defaults.update(overrides)
    return ExceptionCase(**defaults)  # type: ignore[arg-type]


def test_config_stamps_provenance_on_every_recommendation() -> None:
    rec = recommend(_case(), _config(), now=NOW)
    assert rec.policy_version == CONFIG_VERSION


def test_confidence_in_retry_band_recommends_retry() -> None:
    rec = recommend(_case(confidence=0.6), _config(), now=NOW)
    assert rec.action == Action.RETRY


def test_confidence_at_or_above_human_review_threshold_recommends_human_review() -> None:
    rec = recommend(_case(confidence=0.8), _config(), now=NOW)
    assert rec.action == Action.HUMAN_REVIEW


def test_confidence_below_retry_floor_recommends_human_review() -> None:
    rec = recommend(_case(confidence=0.2), _config(), now=NOW)
    assert rec.action == Action.HUMAN_REVIEW


def test_missing_confidence_is_insufficient_evidence_never_a_guess() -> None:
    rec = recommend(_case(confidence=None), _config(), now=NOW)
    assert rec.action == Action.INSUFFICIENT_EVIDENCE


def test_unsupported_failure_type_is_insufficient_evidence() -> None:
    rec = recommend(_case(failure_type="unknown_failure_mode"), _config(), now=NOW)
    assert rec.action == Action.INSUFFICIENT_EVIDENCE


def test_failure_type_not_in_eligible_set_is_insufficient_evidence() -> None:
    config = _config(eligible_failure_types=frozenset({FailureType.LOW_CONFIDENCE.value}))
    rec = recommend(_case(failure_type=FailureType.VALIDATION_CHECK_FAILED.value), config, now=NOW)
    assert rec.action == Action.INSUFFICIENT_EVIDENCE


def test_validation_check_failed_without_recorded_false_is_insufficient_evidence() -> None:
    case = _case(
        failure_type=FailureType.VALIDATION_CHECK_FAILED.value,
        validation_check_passed=None,
    )
    rec = recommend(case, _config(), now=NOW)
    assert rec.action == Action.INSUFFICIENT_EVIDENCE


def test_validation_check_failed_with_recorded_false_is_eligible_for_retry_band() -> None:
    case = _case(
        failure_type=FailureType.VALIDATION_CHECK_FAILED.value,
        validation_check_passed=False,
        confidence=0.6,
    )
    rec = recommend(case, _config(), now=NOW)
    assert rec.action == Action.RETRY


def test_cost_so_far_over_limit_forces_human_review() -> None:
    case = _case(confidence=0.6, cost_so_far_usd=Decimal("5.00"))
    rec = recommend(case, _config(max_retry_cost_usd=Decimal("1.00")), now=NOW)
    assert rec.action == Action.HUMAN_REVIEW
    assert "max_retry_cost_usd" in rec.reason


def test_stale_case_past_deadline_forces_human_review_even_in_retry_band() -> None:
    case = _case(confidence=0.6, opened_at=NOW - timedelta(hours=2))
    rec = recommend(case, _config(decision_deadline_seconds=3600.0), now=NOW)
    assert rec.action == Action.HUMAN_REVIEW
    assert "deadline" in rec.reason


def test_no_opened_at_never_triggers_deadline_check() -> None:
    case = _case(confidence=0.6, opened_at=None)
    rec = recommend(case, _config(decision_deadline_seconds=1.0), now=NOW)
    assert rec.action == Action.RETRY


@pytest.mark.parametrize(
    "kwargs",
    [
        {"retry_floor": -0.1},
        {"human_review_threshold": 1.1},
        {"retry_floor": 0.9, "human_review_threshold": 0.1},
        {"max_retry_cost_usd": Decimal("-1")},
        {"decision_deadline_seconds": 0},
        {"max_retries": 2},
        {"fallback_action": Action.RETRY},
        {"eligible_failure_types": frozenset({"not_a_real_type"})},
    ],
)
def test_invalid_config_is_rejected_at_construction(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _config(**kwargs)


def test_authorize_retry_cost_below_limit() -> None:
    authorized, reason = authorize_retry_cost(
        estimate=CostEstimate(amount_usd=Decimal("0.50"), basis="test"),
        max_retry_cost_usd=Decimal("1.00"),
    )
    assert authorized is True
    assert "0.50" in reason


def test_authorize_retry_cost_above_limit() -> None:
    """Reproduced defect: max_retry_cost_usd=$1, prior spend $0.10, but a
    fixture adapter reporting a $10 retry was still invoked and accepted
    -- the old check only compared sunk cost, never the next attempt's
    own cost. This function is the fix: it authorizes (or refuses) the
    next attempt's cost directly."""
    authorized, reason = authorize_retry_cost(
        estimate=CostEstimate(amount_usd=Decimal("10.00"), basis="test"),
        max_retry_cost_usd=Decimal("1.00"),
    )
    assert authorized is False
    assert "10.00" in reason and "1.00" in reason


def test_authorize_retry_cost_unknown_estimate_is_not_authorized() -> None:
    authorized, reason = authorize_retry_cost(
        estimate=None, max_retry_cost_usd=Decimal("1.00")
    )
    assert authorized is False
    assert "cannot bound" in reason
