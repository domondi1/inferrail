"""Core data model for AP invoice-exception recovery.

Extends the discipline of Inferrail's own `InferenceReceipt`/`inferrail
work` join (see `docs/adr/0003`, `docs/adr/0005`) to a different domain:
a normalized work identifier, a set of attempts against it, and a
separately-recorded review outcome. Every field that could legitimately
be missing is `None`, never a fabricated zero or default.

**Decision-time inputs are a distinct type from outcome-recording
inputs.** `ExceptionCase` is everything available *before* any action is
taken on one exception — it structurally cannot carry a future outcome,
because no such field exists on the type. `ReviewOutcomeRecord` is what
gets recorded *after* a human review resolves a case, and is never an
input to a policy decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class FailureType(StrEnum):
    """The only failure types this release's policy understands. Any
    other value is unsupported: the engine returns `insufficient_evidence`
    and falls back to human review rather than guessing at an unknown
    failure mode. See product/ap-invoice-exception-recovery.md (private
    repo) "First supported failure types"."""

    LOW_CONFIDENCE = "low_confidence"
    VALIDATION_CHECK_FAILED = "validation_check_failed"


class Action(StrEnum):
    """The only two actions this product's decision chooses between,
    plus the required non-answer when the evidence doesn't support a
    choice."""

    RETRY = "retry"
    HUMAN_REVIEW = "human_review"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class Eligibility(StrEnum):
    """Whether a case's event sequence is one this release's single-shot
    decision model supports at all -- computed once per case, before any
    policy is asked to recommend anything."""

    ELIGIBLE = "eligible"
    ALREADY_RESOLVED = "already_resolved"
    """The checkpoint attempt already succeeded -- there is no exception
    to route."""
    UNSUPPORTED_SEQUENCE = "unsupported_sequence"
    """The record's events don't fit the sequence this release's model
    covers -- quarantined rather than fed to a policy with an inferred,
    unsupported interpretation."""


class AttemptStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"
    """The retry adapter call was interrupted (crash/timeout) after
    invocation but before its result was durably recorded. Neither
    success nor failure is asserted -- never silently retried again
    (which could re-invoke a real paid provider) and never assumed
    successful."""


class ReviewOutcome(StrEnum):
    ACCEPTED = "accepted"
    CORRECTED = "corrected"
    REJECTED = "rejected"
    ESCALATED = "escalated"


class DecisionStatus(StrEnum):
    """Lifecycle status of one work_id's checkpoint decision."""

    RETRY_IN_PROGRESS = "retry_in_progress"
    RETRY_RESOLVED = "retry_resolved"
    """A retry ran and its own outcome (success/failed/ambiguous) is
    known; may still be followed by a human review if the retry didn't
    resolve the exception."""
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    RESOLVED = "resolved"
    """A human-review outcome has been recorded."""


@dataclass(frozen=True)
class ExceptionCase:
    """Everything available at decision time for one invoice exception.

    Structurally excludes every future/outcome field -- there is no
    "what happened" field on this type. `confidence` and prior
    deterministic check results are whatever the vendor's own extraction
    pipeline already produced; this type does not compute them.
    """

    work_id: str
    checkpoint_attempt_id: str
    failure_type: str
    """Free-text as received; only values matching `FailureType` are
    eligible for a policy recommendation -- see `policy.classify_eligibility`."""
    confidence: float | None = None
    validation_check_passed: bool | None = None
    """Result of whatever deterministic cross-check the vendor already
    ran on the checkpoint attempt, if any -- `None` if the vendor did not
    run one (not the same as `False`)."""
    cost_so_far_usd: Decimal | None = None
    opened_at: datetime | None = None
    source: str = "unknown"
    extraction_policy_version: str | None = None

    def __post_init__(self) -> None:
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1]: {self.confidence!r}")
        if self.cost_so_far_usd is not None and self.cost_so_far_usd < 0:
            raise ValueError(f"cost_so_far_usd cannot be negative: {self.cost_so_far_usd!r}")


@dataclass(frozen=True)
class RetryAttemptResult:
    """What a `RetryAdapter` returns after making one retry attempt."""

    attempt_id: str
    status: AttemptStatus
    cost_usd: Decimal | None
    confidence: float | None = None
    provider: str = "unknown"
    raw_fields: dict[str, str] = field(default_factory=dict)
    """Re-extracted field values, if the adapter chooses to surface them.
    Never persisted by the engine beyond what the customer's own callback
    does with them -- the store keeps only status/cost/confidence, never
    invoice field values."""

    def __post_init__(self) -> None:
        if self.cost_usd is not None and self.cost_usd < 0:
            raise ValueError(f"cost_usd cannot be negative: {self.cost_usd!r}")
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1]: {self.confidence!r}")


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class ValidationResult:
    """Result of running the declared validation rules against a retry's
    output. **This is not the same as independently established
    correctness** -- it means the declared checks passed, nothing more.
    Independently established correctness comes only from a recorded
    human-review outcome or the vendor's own downstream reconciliation.
    """

    passed: bool
    checks: tuple[CheckResult, ...]
    validator_version: str

    @property
    def failed_checks(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if not c.passed)


@dataclass(frozen=True)
class Recommendation:
    """One policy's answer for one case, plus why."""

    work_id: str
    policy_name: str
    policy_version: str
    action: Action
    reason: str


@dataclass(frozen=True)
class ReviewOutcomeRecord:
    """What actually happened to one exception after human review --
    recorded only after the fact, never an input to a policy decision."""

    work_id: str
    outcome: ReviewOutcome
    timestamp: datetime
    source: str = "unknown"
    correction_delta_usd: Decimal | None = None
    review_cost_usd: Decimal | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("correction_delta_usd", self.correction_delta_usd),
            ("review_cost_usd", self.review_cost_usd),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative: {value!r}")
