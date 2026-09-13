"""Validation contract: checks a retry attempt's result against declared
rules. See `models.ValidationResult` for the distinction this module
exists to protect: **a passed validation means the declared checks
passed -- it is not independently established correctness.** Correctness
is only established later, by a recorded human-review outcome or the
vendor's own downstream reconciliation.
"""

from __future__ import annotations

from typing import Protocol

from .models import AttemptStatus, CheckResult, RetryAttemptResult, ValidationResult

VALIDATOR_VERSION = "ap.validator/v1"


class Validator(Protocol):
    def validate(self, result: RetryAttemptResult) -> ValidationResult: ...


class FieldPresenceAndConfidenceValidator:
    """Reference validator: checks that the retry attempt reports
    `SUCCESS`, that its confidence (if any) meets a floor, and that it
    did not come back `AMBIGUOUS`. Deliberately simple and generic --
    field-level content checks (e.g. arithmetic cross-checks on
    specific invoice fields) are the customer's own responsibility via a
    custom `Validator`, since this module has no knowledge of what
    fields a given vendor's invoices carry.
    """

    def __init__(self, *, min_confidence: float | None = None) -> None:
        self._min_confidence = min_confidence

    def validate(self, result: RetryAttemptResult) -> ValidationResult:
        checks: list[CheckResult] = []

        checks.append(
            CheckResult(
                name="attempt_status_not_ambiguous",
                passed=result.status != AttemptStatus.AMBIGUOUS,
                detail=f"status={result.status.value}",
            )
        )
        checks.append(
            CheckResult(
                name="attempt_status_success",
                passed=result.status == AttemptStatus.SUCCESS,
                detail=f"status={result.status.value}",
            )
        )
        if self._min_confidence is not None:
            confidence = result.confidence
            passed = confidence is not None and confidence >= self._min_confidence
            checks.append(
                CheckResult(
                    name="confidence_at_or_above_floor",
                    passed=passed,
                    detail=f"confidence={confidence!r}, floor={self._min_confidence!r}",
                )
            )

        return ValidationResult(
            passed=all(c.passed for c in checks),
            checks=tuple(checks),
            validator_version=VALIDATOR_VERSION,
        )
