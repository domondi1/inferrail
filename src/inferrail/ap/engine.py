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

import os
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from .adapters import RetryAdapter, get_cost_estimate
from .handoff import HandoffSendFailed, HumanReviewHandoff
from .models import Action, AttemptStatus, DecisionStatus, ExceptionCase, Recommendation
from .policy import PolicyConfig, authorize_retry_cost, recommend
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
    raw_fields: dict[str, str] = field(default_factory=dict)
    """Re-extracted, usable field values from a *validated-successful*
    retry only -- see `RecoveryEngine._execute_retry`. Never populated on
    `_replay` (the store doesn't persist field values by design; see
    `models.RetryAttemptResult`) -- a caller needing them again on a
    repeat call must keep them itself, same as raw invoice content."""
    late_result_recorded: bool = False
    """True when this call's own retry result arrived after this
    work_id's lease had already been reaped by
    `RecoveryEngine.reap_stale_retries` -- the real result is preserved
    in the store's `late_retry_results` audit trail, but the
    authoritative decision status is not silently flipped back to
    `retry_resolved`; see `RecoveryEngine._execute_retry`."""


class RecoveryEngine:
    def __init__(
        self,
        *,
        store: RecoveryStore,
        config: PolicyConfig,
        retry_adapter: RetryAdapter,
        validator: Validator,
        handoff: HumanReviewHandoff,
        worker_id: str | None = None,
        retry_lease_seconds: float = 120.0,
    ) -> None:
        self._store = store
        self._config = config
        self._retry_adapter = retry_adapter
        self._validator = validator
        self._handoff = handoff
        self._worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
        self._retry_lease_seconds = retry_lease_seconds

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
        is_retry = recommendation.action == Action.RETRY
        initial_status = (
            DecisionStatus.RETRY_IN_PROGRESS.value if is_retry
            else DecisionStatus.AWAITING_HUMAN_REVIEW.value
        )
        clock = now or datetime.now(UTC)
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
            worker_id=self._worker_id if is_retry else None,
            lease_expires_at=(
                clock.timestamp() + self._retry_lease_seconds if is_retry else None
            ),
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
                status=DecisionStatus.AWAITING_HUMAN_REVIEW.value,
                idempotent_replay=False,
                handoff_ref=handoff_ref,
            )

        return self._execute_retry(case, decision_id, recommendation)

    def _execute_retry(
        self, case: ExceptionCase, decision_id: str, recommendation: Recommendation
    ) -> DecisionResult:
        estimate = get_cost_estimate(self._retry_adapter, case)
        authorized, auth_reason = authorize_retry_cost(
            estimate=estimate, max_retry_cost_usd=self._config.max_retry_cost_usd
        )
        if not authorized:
            # The adapter is never invoked when its next attempt's cost
            # cannot be authorized -- no retry_attempts row is written,
            # so the eventual report honestly shows no retry was ever
            # attempted, not a failed one.
            self._store.set_decision_status(
                case.work_id, DecisionStatus.AWAITING_HUMAN_REVIEW.value
            )
            self._store.clear_retry_lease(case.work_id)
            handoff_ref = self._do_handoff(
                case,
                recommendation,
                attempt_history=({"detail": auth_reason},),
            )
            return DecisionResult(
                work_id=case.work_id,
                decision_id=decision_id,
                recommended_action=Action.RETRY,
                reason=recommendation.reason,
                policy_version=recommendation.policy_version,
                status=DecisionStatus.AWAITING_HUMAN_REVIEW.value,
                idempotent_replay=False,
                handoff_ref=handoff_ref,
            )

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
            self._store.set_decision_status(
                case.work_id, DecisionStatus.AWAITING_HUMAN_REVIEW.value
            )
            self._store.clear_retry_lease(case.work_id)
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
                status=DecisionStatus.AWAITING_HUMAN_REVIEW.value,
                idempotent_replay=False,
                retry_status=AttemptStatus.AMBIGUOUS,
                handoff_ref=handoff_ref,
            )

        validation = self._validator.validate(result)
        try:
            self._store.record_retry_attempt(
                work_id=case.work_id,
                attempt_id=result.attempt_id,
                status=result.status.value,
                cost_usd=str(result.cost_usd) if result.cost_usd is not None else None,
                confidence=str(result.confidence) if result.confidence is not None else None,
                provider=result.provider,
                validation_passed=str(validation.passed),
                validator_version=validation.validator_version,
                pre_flight_estimate_usd=str(estimate.amount_usd) if estimate else None,
            )
        except AmbiguousRetryError:
            # This work_id's lease was already reaped (a synthetic
            # "ambiguous" attempt is already recorded) while this call
            # was actually still running -- the real result is never
            # lost, but the decision's authoritative status is never
            # silently flipped back to retry_resolved on this path; see
            # `reap_stale_retries`.
            self._store.record_late_retry_result(
                work_id=case.work_id,
                attempt_id=result.attempt_id,
                status=result.status.value,
                cost_usd=str(result.cost_usd) if result.cost_usd is not None else None,
                provider=result.provider,
                detail="arrived after this work_id's lease was already reaped",
            )
            current = self._store.get_decision(case.work_id)
            assert current is not None
            return DecisionResult(
                work_id=case.work_id,
                decision_id=decision_id,
                recommended_action=Action.RETRY,
                reason=recommendation.reason,
                policy_version=recommendation.policy_version,
                status=current["status"],
                idempotent_replay=False,
                retry_status=result.status,
                retry_cost_usd=result.cost_usd,
                late_result_recorded=True,
            )

        if validation.passed:
            self._store.set_decision_status(case.work_id, DecisionStatus.RETRY_RESOLVED.value)
            self._store.clear_retry_lease(case.work_id)
            return DecisionResult(
                work_id=case.work_id,
                decision_id=decision_id,
                recommended_action=Action.RETRY,
                reason=recommendation.reason,
                policy_version=recommendation.policy_version,
                status=DecisionStatus.RETRY_RESOLVED.value,
                idempotent_replay=False,
                retry_status=result.status,
                retry_cost_usd=result.cost_usd,
                raw_fields=dict(result.raw_fields),
            )

        # Retry ran but did not pass validation -- falls back to the
        # established human-review path, per config.fallback_action.
        self._store.set_decision_status(case.work_id, DecisionStatus.AWAITING_HUMAN_REVIEW.value)
        self._store.clear_retry_lease(case.work_id)
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
            status=DecisionStatus.AWAITING_HUMAN_REVIEW.value,
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
        try:
            handoff_ref = self._handoff.send(case, recommendation, attempt_history)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, re-raised typed below
            # No handoff row is recorded (a stored status must never
            # claim a handoff succeeded when no receiving system
            # acknowledged it) -- a caller should retry via
            # `ensure_handoff` once the vendor's system is reachable
            # again, which re-checks for an existing handoff first, same
            # as this method does above.
            raise HandoffSendFailed(
                f"handoff callback raised for work_id={case.work_id!r}: {exc!r}"
            ) from exc
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

    def reap_stale_retries(self, *, now: datetime | None = None) -> list[dict[str, str]]:
        """Durable crash recovery: finds every decision whose retry
        lease has expired (its worker is presumed dead -- never silently
        retried, never assumed successful) and moves each to
        `awaiting_human_review` with a synthetic `ambiguous` attempt
        recorded. Idempotent -- a repeat call reaps nothing new for a
        work_id already reaped or already resolved by its real worker
        (see `store.reap_stale_retry_lease`).

        Deliberately does **not** send a handoff itself: the `decisions`
        table doesn't persist the `opened_at`/`source`/etc. fields needed
        to reconstruct an `ExceptionCase`, and fabricating one would
        violate this codebase's "never guess" discipline. A caller (an
        operator's own recovery job, or `inferrail ap reap`) that
        re-supplies the original case can complete the handoff via
        `ensure_handoff`.
        """
        ts = now.timestamp() if now is not None else None
        reaped: list[dict[str, str]] = []
        for row in self._store.find_stale_retry_leases(now=ts):
            result = self._store.reap_stale_retry_lease(row["work_id"], now=ts)
            if result is not None:
                reaped.append(
                    {
                        "work_id": row["work_id"],
                        "decision_id": row["decision_id"],
                        "reaped_attempt_id": result["attempt_id"],
                    }
                )
        return reaped

    def ensure_handoff(self, case: ExceptionCase) -> str:
        """Idempotently completes (or retries) a handoff for a work_id
        whose decision requires human review -- for a caller recovering
        from a reaped lease (see `reap_stale_retries`) or retrying after
        `_do_handoff` raised `HandoffSendFailed`. Reuses the same
        "already has a handoff" guard `_do_handoff` uses internally, so
        calling this twice never re-sends. Raises `ValueError` for a
        work_id with no decision, or one whose status never calls for a
        handoff (`retry_resolved`/`resolved`) -- an unsupported
        transition is rejected rather than silently accepted."""
        row = self._store.get_decision(case.work_id)
        if row is None:
            raise ValueError(f"no decision recorded for work_id={case.work_id!r}")
        if row["status"] not in (
            DecisionStatus.AWAITING_HUMAN_REVIEW.value,
        ):
            raise ValueError(
                f"work_id={case.work_id!r} has status {row['status']!r}, "
                "which does not call for a handoff"
            )
        recommendation = Recommendation(
            work_id=case.work_id,
            policy_name=row["policy_name"],
            policy_version=row["policy_version"],
            action=Action(row["recommended_action"]),
            reason=row["reason"],
        )
        attempt = self._store.get_retry_attempt(case.work_id)
        attempt_history = (
            (
                {
                    "attempt_id": attempt["attempt_id"],
                    "status": attempt["status"],
                    "detail": "recovered via ensure_handoff",
                },
            )
            if attempt is not None
            else ()
        )
        return self._do_handoff(case, recommendation, attempt_history)
