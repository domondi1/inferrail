"""Versioned, configurable policy for the AP invoice-exception-recovery
decision: retry, human_review, or insufficient_evidence for one
`ExceptionCase`.

**This policy is not calibrated economic optimization.** `retry_floor`
and `human_review_threshold` are uninterpreted cutoffs on whatever
`confidence` number the vendor's own extraction pipeline reports -- this
module does not assume, and has no way to check, that it is a calibrated
probability of correct resolution. What this policy *uses*: failure
type, confidence, cost-so-far, case age, and the fixed retry/cost/
deadline limits below. What it *guarantees*: at most `max_retries`
machine attempts, a fixed fallback to human review whenever the evidence
doesn't support a recommendation, and no counterfactual is ever
fabricated for an action not taken. Nothing beyond that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from .models import Action, ExceptionCase, FailureType, Recommendation

CONFIG_VERSION = "ap.policy/v1"

_SUPPORTED_FAILURE_TYPES = frozenset(t.value for t in FailureType)


@dataclass(frozen=True)
class PolicyConfig:
    """A versioned, immutable policy configuration. Stamped onto every
    decision (`policy_version`) for provenance -- a report can always
    show exactly which config produced a given recommendation.

    `max_retries` is fixed at `1` in this release (`config_version`
    `ap.policy/v1`) by construction (see `__post_init__`) -- a future
    schema version may raise it; this one deliberately does not, to keep
    the release bounded.
    """

    eligible_failure_types: frozenset[str]
    retry_floor: float
    human_review_threshold: float
    max_retry_cost_usd: Decimal
    decision_deadline_seconds: float
    max_retries: int = 1
    fallback_action: Action = Action.HUMAN_REVIEW
    config_version: str = CONFIG_VERSION
    name: str = "candidate_policy"

    def __post_init__(self) -> None:
        unsupported = self.eligible_failure_types - _SUPPORTED_FAILURE_TYPES
        if unsupported:
            raise ValueError(
                f"eligible_failure_types contains unsupported type(s): {sorted(unsupported)}"
            )
        if not (0.0 <= self.retry_floor <= 1.0):
            raise ValueError(f"retry_floor must be in [0, 1]: {self.retry_floor!r}")
        if not (0.0 <= self.human_review_threshold <= 1.0):
            raise ValueError(
                f"human_review_threshold must be in [0, 1]: {self.human_review_threshold!r}"
            )
        if self.retry_floor > self.human_review_threshold:
            raise ValueError("retry_floor must be <= human_review_threshold")
        if self.max_retry_cost_usd < 0:
            raise ValueError("max_retry_cost_usd cannot be negative")
        if self.decision_deadline_seconds <= 0:
            raise ValueError("decision_deadline_seconds must be positive")
        if self.max_retries != 1:
            raise ValueError(
                "config_version ap.policy/v1 fixes max_retries at 1 by design "
                "(see product/ap-invoice-exception-recovery.md, private repo) "
                "-- a future config_version may relax this"
            )
        if self.fallback_action != Action.HUMAN_REVIEW:
            raise ValueError(
                "config_version ap.policy/v1 fixes fallback_action at human_review -- "
                "the established path, never an invented alternative"
            )


def _case_age_seconds(case: ExceptionCase, *, now: datetime | None = None) -> float | None:
    if case.opened_at is None:
        return None
    current = now or datetime.now(UTC)
    opened = case.opened_at
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return (current - opened).total_seconds()


def recommend(
    case: ExceptionCase,
    config: PolicyConfig,
    *,
    now: datetime | None = None,
) -> Recommendation:
    """The single entry point every decision goes through. Never raises
    on a case it can't confidently decide about -- returns
    `insufficient_evidence` (falling back to `config.fallback_action`,
    fixed at `human_review` in this release) instead of guessing.
    """

    def _rec(action: Action, reason: str) -> Recommendation:
        return Recommendation(
            work_id=case.work_id,
            policy_name=config.name,
            policy_version=config.config_version,
            action=action,
            reason=reason,
        )

    if case.failure_type not in config.eligible_failure_types:
        return _rec(
            Action.INSUFFICIENT_EVIDENCE,
            f"failure_type {case.failure_type!r} is not in this policy's "
            f"eligible_failure_types {sorted(config.eligible_failure_types)} -- "
            "unsupported failure types are never guessed at",
        )

    age = _case_age_seconds(case, now=now)
    if age is not None and age > config.decision_deadline_seconds:
        return _rec(
            Action.HUMAN_REVIEW,
            f"case age {age:.0f}s exceeds decision_deadline_seconds "
            f"{config.decision_deadline_seconds:.0f}s -- stale exceptions are not retried",
        )

    if case.confidence is None:
        return _rec(
            Action.INSUFFICIENT_EVIDENCE,
            "no confidence value on the checkpoint attempt",
        )

    if case.failure_type == FailureType.VALIDATION_CHECK_FAILED.value:
        if case.validation_check_passed is not False:
            return _rec(
                Action.INSUFFICIENT_EVIDENCE,
                "failure_type is validation_check_failed but "
                "validation_check_passed is not recorded as False",
            )

    if case.cost_so_far_usd is not None and case.cost_so_far_usd > config.max_retry_cost_usd:
        return _rec(
            Action.HUMAN_REVIEW,
            f"cost so far ${case.cost_so_far_usd} exceeds max_retry_cost_usd "
            f"${config.max_retry_cost_usd} -- not worth an additional attempt",
        )

    if config.retry_floor <= case.confidence < config.human_review_threshold:
        return _rec(
            Action.RETRY,
            f"confidence {case.confidence:.2f} in the retry band "
            f"[{config.retry_floor:.2f}, {config.human_review_threshold:.2f})",
        )

    return _rec(
        Action.HUMAN_REVIEW,
        f"confidence {case.confidence:.2f} outside the retry band -- "
        f"route to {config.fallback_action.value}",
    )
