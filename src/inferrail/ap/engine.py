"""The recovery engine: decides retry-vs-human-review for one
`ExceptionCase`, executes the retry through the customer-supplied
adapter, validates it, hands off to human review when needed, and
persists every step durably and idempotently.

This is the one place all of this release's pieces (`policy`,
`adapters`, `validation`, `handoff`, `store`) are wired together. Runs
entirely in the customer's own process -- invoice content and provider
credentials never leave it (see `adapters.RetryAdapter`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from .adapters import RetryAdapter
from .handoff import HumanReviewHandoff
from .models import Action, AttemptStatus, ExceptionCase, Recommendation
from .policy import PolicyConfig, recommend
from .store import AmbiguousRetryError, RecoveryStore
from .validation import Validator


def _dec(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class DecisionResult:
    """What `RecoveryEngine.decide` returns -- one row's worth of the
    eventual auditable report (see `report.py`)."""

    work_id: str
    decision_id: str
    recommended_action: Action
    reason: str
    policy_version: str
    status: str
    idempotent_replay: bool
    """True when this call found an existing decision for `work_id` and
    returned it unchanged -- the policy was not re-evaluated and no
    adapter/handoff callback was invoked again."""
    retry_status: AttemptStatus | None = None
    retry_cost_usd: Decimal | None = None
    handoff_ref: str | None = None


class RecoveryEngine:
    def __init__(
        self,
        *,
        store: RecoveryStore,
        config: PolicyConfig,
        retry_adapter: RetryAdapter,
        validator: Validator,
        handoff: HumanReviewHandoff,
    ) -> None:
        self._store = store
        self._config = config
        self._retry_adapter = retry_adapter
        self._validator = validator
        self._handoff = handoff

    def decide(self, case: ExceptionCase, *, now: datetime | None = None) -> DecisionResult:
        """Idempotent on `case.work_id`: a repeated call for a work_id
        that already has a decision returns the stored result without
        re-evaluating the policy or re-invoking the retry adapter or
        handoff callback -- see the demo scenario "a repeated request
        handled without silently repeating the action". `now` fixes the
        clock used for `PolicyConfig.decision_deadline_seconds` -- pass it
        explicitly for deterministic/reproducible decisions (tests, the
        fixture demo); omitted, it defaults to the real wall-clock time,
        which is what production use wants."""
        existing = self._store.get_decision(case.work_id)
        if existing is not None:
            return self._replay(existing)

        recommendation = recommend(case, self._config, now=now)
        decision_id = f"dec_{uuid4().hex[:12]}"
        initial_status = (
            "retry_in_progress" if recommendation.action == Action.RETRY
            else "awaiting_human_review"
        )
        row, created = self._store.create_decision(
            work_id=case.work_id,
            decision_id=decision_id,
            checkpoint_attempt_id=case.checkpoint_attempt_id,
            failure_type=case.failure_type,
            confidence=str(case.confidence) if case.confidence is not None else None,
            cost_so_far_usd=str(case.cost_so_far_usd) if case.cost_so_far_usd is not None else None,
            policy_name=recommendation.policy_name,
            policy_version=recommendation.policy_version,
            recommended_action=recommendation.action.value,
            reason=recommendation.reason,
            status=initial_status,
        )
        if not created:
            # Lost a race with a concurrent caller for the same work_id --
            # never proceed with a second execution of our own.
            return self._replay(row)

        if recommendation.action != Action.RETRY:
            handoff_ref = self._do_handoff(case, recommendation, attempt_history=())
            return DecisionResult(
                work_id=case.work_id,
                decision_id=decision_id,
                recommended_action=recommendation.action,
                reason=recommendation.reason,
                policy_version=recommendation.policy_version,
                status="awaiting_human_review",
                idempotent_replay=False,
                handoff_ref=handoff_ref,
            )

        return self._execute_retry(case, decision_id, recommendation)

    def _execute_retry(
        self, case: ExceptionCase, decision_id: str, recommendation: Recommendation
    ) -> DecisionResult:
        try:
            result = self._retry_adapter.retry(case)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: any
            # interruption during the adapter call means the underlying
            # action's real-world outcome is unknown, not that it failed.
            attempt_id = f"ret_ambiguous_{uuid4().hex[:12]}"
            try:
                self._store.record_retry_attempt(
                    work_id=case.work_id,
                    attempt_id=attempt_id,
                    status=AttemptStatus.AMBIGUOUS.value,
                    cost_usd=None,
                    confidence=None,
                    provider=getattr(self._retry_adapter, "name", "unknown"),
                )
            except AmbiguousRetryError:
                pass
            self._store.set_decision_status(case.work_id, "awaiting_human_review")
            handoff_ref = self._do_handoff(
                case,
                recommendation,
                attempt_history=(
                    {
                        "attempt_id": attempt_id,
                        "status": AttemptStatus.AMBIGUOUS.value,
                        "detail": (
                            f"retry adapter raised {exc!r} -- outcome unknown, "
                            "never retried again"
                        ),
                    },
                ),
            )
            return DecisionResult(
                work_id=case.work_id,
                decision_id=decision_id,
                recommended_action=Action.RETRY,
                reason=recommendation.reason,
                policy_version=recommendation.policy_version,
                status="awaiting_human_review",
                idempotent_replay=False,
                retry_status=AttemptStatus.AMBIGUOUS,
                handoff_ref=handoff_ref,
            )

        validation = self._validator.validate(result)
        self._store.record_retry_attempt(
            work_id=case.work_id,
            attempt_id=result.attempt_id,
            status=result.status.value,
            cost_usd=str(result.cost_usd) if result.cost_usd is not None else None,
            confidence=str(result.confidence) if result.confidence is not None else None,
            provider=result.provider,
            validation_passed=str(validation.passed),
            validator_version=validation.validator_version,
        )

        if validation.passed:
            self._store.set_decision_status(case.work_id, "retry_resolved")
            return DecisionResult(
                work_id=case.work_id,
                decision_id=decision_id,
                recommended_action=Action.RETRY,
                reason=recommendation.reason,
                policy_version=recommendation.policy_version,
                status="retry_resolved",
                idempotent_replay=False,
                retry_status=result.status,
                retry_cost_usd=result.cost_usd,
            )

        # Retry ran but did not pass validation -- falls back to the
        # established human-review path, per config.fallback_action.
        self._store.set_decision_status(case.work_id, "awaiting_human_review")
        handoff_ref = self._do_handoff(
            case,
            recommendation,
            attempt_history=(
                {
                    "attempt_id": result.attempt_id,
                    "status": result.status.value,
                    "detail": (
                        "retry attempt did not pass validation: "
                        f"{[c.name for c in validation.failed_checks]}"
                    ),
                },
            ),
        )
        return DecisionResult(
            work_id=case.work_id,
            decision_id=decision_id,
            recommended_action=Action.RETRY,
            reason=recommendation.reason,
            policy_version=recommendation.policy_version,
            status="awaiting_human_review",
            idempotent_replay=False,
            retry_status=result.status,
            retry_cost_usd=result.cost_usd,
            handoff_ref=handoff_ref,
        )

    def _do_handoff(
        self,
        case: ExceptionCase,
        recommendation: Recommendation,
        attempt_history: tuple[dict[str, Any], ...],
    ) -> str:
        existing = self._store.get_handoff(case.work_id)
        if existing is not None:
            return str(existing["handoff_ref"])
        handoff_ref = self._handoff.send(case, recommendation, attempt_history)
        row, _created = self._store.record_handoff(work_id=case.work_id, handoff_ref=handoff_ref)
        return str(row["handoff_ref"])

    def _replay(self, row: dict[str, Any]) -> DecisionResult:
        attempt = self._store.get_retry_attempt(row["work_id"])
        handoff = self._store.get_handoff(row["work_id"])
        return DecisionResult(
            work_id=row["work_id"],
            decision_id=row["decision_id"],
            recommended_action=Action(row["recommended_action"]),
            reason=row["reason"],
            policy_version=row["policy_version"],
            status=row["status"],
            idempotent_replay=True,
            retry_status=AttemptStatus(attempt["status"]) if attempt else None,
            retry_cost_usd=_dec(attempt["cost_usd"]) if attempt else None,
            handoff_ref=str(handoff["handoff_ref"]) if handoff else None,
        )

    def record_outcome(
        self,
        *,
        work_id: str,
        outcome: str,
        timestamp: float,
        source: str = "unknown",
        correction_delta_usd: Decimal | None = None,
        review_cost_usd: Decimal | None = None,
    ) -> dict[str, Any]:
        """Records the vendor's established human-review path's real
        result. Independently establishes correctness for this work_id
        -- distinct from, and never inferred from, any earlier
        `ValidationResult`."""
        return self._store.record_outcome(
            work_id=work_id,
            outcome=outcome,
            timestamp=timestamp,
            source=source,
            correction_delta_usd=(
                str(correction_delta_usd) if correction_delta_usd is not None else None
            ),
            review_cost_usd=str(review_cost_usd) if review_cost_usd is not None else None,
        )
