"""The inspectable report for the live decision path: joins every
persisted decision, retry attempt, handoff, and recorded outcome from a
`RecoveryStore` into one auditable view.

Same cost-boundary discipline as `batch.py` (ported from the superseded
prototype): `sunk_cost_usd` is the checkpoint's already-spent cost,
identical no matter what was decided; `observed_cost_usd` is the
incremental cost actually caused by what happened (the retry's own cost,
plus a subsequent human review's cost when the retry still needed one).
`validation_result` and `established_outcome` are kept as distinct
fields -- a passed validation is never presented as, or merged with,
independently established correctness (see `validation.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import DecisionStatus
from .store import RecoveryStore

_TERMINAL_STATUSES = frozenset(
    {DecisionStatus.RETRY_RESOLVED.value, DecisionStatus.RESOLVED.value}
)


def _dec(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class LiveReportRow:
    work_id: str
    decision_id: str
    checkpoint_attempt_id: str
    failure_type: str
    recommended_action: str
    reason: str
    policy_version: str
    status: str
    sunk_cost_usd: Decimal | None
    retry_attempt_id: str | None
    retry_status: str | None
    retry_cost_usd: Decimal | None
    validation_passed: bool | None
    validator_version: str | None
    handoff_ref: str | None
    established_outcome: str | None
    """From a recorded `ReviewOutcomeRecord` -- independently established
    correctness. `None` until a real human-review result is recorded, no
    matter what `validation_passed` says."""
    review_cost_usd: Decimal | None
    """The latest recorded review's own cost, if the customer tracks and
    supplies it -- kept as its own field (previously only folded into
    `observed_cost_usd`'s sum) so a reader can see it, and so
    `work_economics_export` can report it as a cost component distinct
    from the machine-attempt cost."""
    pre_flight_estimate_usd: Decimal | None
    """The retry adapter's own `CostEstimate.amount_usd` that authorized
    this attempt, if one was made -- see `policy.authorize_retry_cost`."""
    retry_cost_overrun_usd: Decimal | None
    """`retry_cost_usd - pre_flight_estimate_usd` when the real charge
    exceeded the pre-flight estimate that authorized it -- never clamped
    or hidden; `None` when there's no overrun (or nothing to compare)."""
    observed_cost_usd: Decimal | None
    observed_cost_complete: bool
    """`True` only once the decision has reached a terminal status
    (`retry_resolved`/`resolved`) *and* every cost component that
    applies (a retry's own cost when one was attempted; a review's own
    cost when one was recorded) is actually known -- never inferred from
    a retry's reported status alone. See `_observed_cost`."""
    outcome_revision_count: int
    """>1 means a later correction superseded an earlier recorded
    outcome -- both are preserved in the store's append-only outcome
    history; only the latest is authoritative here."""
    late_result_status: str | None
    """The status of a real retry result that arrived after this
    work_id's lease was already reaped (see `engine.RecoveryEngine.
    _execute_retry`'s `AmbiguousRetryError` handling and `store.
    late_retry_results`) -- surfaced here for audit visibility, exactly
    once (the latest late arrival, if more than one is ever recorded).
    `None` when no late result exists. **Never folded into
    `observed_cost_usd`/`observed_cost_complete`** -- the decision's
    authoritative status (`status`, above) was never promoted by this
    signal, and neither is the cost total; this field exists so a
    reader can see what the late signal reported without the report
    silently treating it as confirmed."""
    late_result_cost_usd: Decimal | None
    """The cost reported by that same late result, if known -- purely
    informational, deliberately excluded from `observed_cost_usd`. See
    `late_result_status`."""


@dataclass(frozen=True)
class LiveAuditableReport:
    rows: tuple[LiveReportRow, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": [
                {
                    "work_id": r.work_id,
                    "decision_id": r.decision_id,
                    "checkpoint_attempt_id": r.checkpoint_attempt_id,
                    "failure_type": r.failure_type,
                    "recommended_action": r.recommended_action,
                    "reason": r.reason,
                    "policy_version": r.policy_version,
                    "status": r.status,
                    "sunk_cost_usd": str(r.sunk_cost_usd) if r.sunk_cost_usd is not None else None,
                    "retry_attempt_id": r.retry_attempt_id,
                    "retry_status": r.retry_status,
                    "retry_cost_usd": (
                        str(r.retry_cost_usd) if r.retry_cost_usd is not None else None
                    ),
                    "validation_passed": r.validation_passed,
                    "validator_version": r.validator_version,
                    "handoff_ref": r.handoff_ref,
                    "established_outcome": r.established_outcome,
                    "review_cost_usd": (
                        str(r.review_cost_usd) if r.review_cost_usd is not None else None
                    ),
                    "pre_flight_estimate_usd": (
                        str(r.pre_flight_estimate_usd)
                        if r.pre_flight_estimate_usd is not None
                        else None
                    ),
                    "retry_cost_overrun_usd": (
                        str(r.retry_cost_overrun_usd)
                        if r.retry_cost_overrun_usd is not None
                        else None
                    ),
                    "observed_cost_usd": (
                        str(r.observed_cost_usd) if r.observed_cost_usd is not None else None
                    ),
                    "observed_cost_complete": r.observed_cost_complete,
                    "outcome_revision_count": r.outcome_revision_count,
                    "late_result_status": r.late_result_status,
                    "late_result_cost_usd": (
                        str(r.late_result_cost_usd)
                        if r.late_result_cost_usd is not None
                        else None
                    ),
                }
                for r in self.rows
            ]
        }


def _observed_cost(
    *,
    decision_status: str,
    recommended_action: str,
    retry_status: str | None,
    retry_cost_usd: Decimal | None,
    review_cost_usd: Decimal | None,
    has_outcome: bool,
) -> tuple[Decimal | None, bool]:
    """Reproduced defect (fixed here): a retry that reports
    `status="success"` but still fails *validation* used to be marked
    complete anyway (completeness was inferred from `retry_status`
    alone, which says nothing about validation or what happened next).
    Completeness is now keyed off the decision's own lifecycle status --
    a case is only ever complete once it has actually reached a terminal
    state (`retry_resolved`/`resolved`), and even then only if every
    cost component that applies is actually known. A missing review cost
    is incomplete regardless of `recommended_action` (previously the
    check was skipped entirely for `recommended_action == "retry"`, so a
    retry-then-review case with an unknown review cost was also wrongly
    marked complete)."""
    components: list[Decimal] = []

    if recommended_action == "retry" and retry_cost_usd is not None:
        components.append(retry_cost_usd)
    if has_outcome and review_cost_usd is not None:
        components.append(review_cost_usd)

    complete = decision_status in _TERMINAL_STATUSES
    if recommended_action == "retry" and retry_status is not None and retry_cost_usd is None:
        complete = False
    if has_outcome and review_cost_usd is None:
        complete = False

    if not components:
        return (None, complete)
    total = Decimal(0)
    for c in components:
        total += c
    return (total, complete)


def build_live_report(store: RecoveryStore) -> LiveAuditableReport:
    rows: list[LiveReportRow] = []
    for work_id in store.all_work_ids():
        decision = store.get_decision(work_id)
        assert decision is not None
        attempt = store.get_retry_attempt(work_id)
        handoff = store.get_handoff(work_id)
        outcome_history = store.get_outcome_history(work_id)
        latest_outcome = outcome_history[-1] if outcome_history else None

        late_results = store.get_late_retry_results(work_id)
        latest_late = late_results[-1] if late_results else None

        review_cost = _dec(latest_outcome["review_cost_usd"]) if latest_outcome else None
        retry_cost = _dec(attempt["cost_usd"]) if attempt else None
        pre_flight_estimate = (
            _dec(attempt.get("pre_flight_estimate_usd")) if attempt else None
        )
        overrun = (
            retry_cost - pre_flight_estimate
            if retry_cost is not None
            and pre_flight_estimate is not None
            and retry_cost > pre_flight_estimate
            else None
        )
        observed_cost, complete = _observed_cost(
            decision_status=decision["status"],
            recommended_action=decision["recommended_action"],
            retry_status=attempt["status"] if attempt else None,
            retry_cost_usd=retry_cost,
            review_cost_usd=review_cost,
            has_outcome=latest_outcome is not None,
        )

        rows.append(
            LiveReportRow(
                work_id=work_id,
                decision_id=decision["decision_id"],
                checkpoint_attempt_id=decision["checkpoint_attempt_id"],
                failure_type=decision["failure_type"],
                recommended_action=decision["recommended_action"],
                reason=decision["reason"],
                policy_version=decision["policy_version"],
                status=decision["status"],
                sunk_cost_usd=_dec(decision["cost_so_far_usd"]),
                retry_attempt_id=attempt["attempt_id"] if attempt else None,
                retry_status=attempt["status"] if attempt else None,
                retry_cost_usd=retry_cost,
                validation_passed=(
                    attempt["validation_passed"] == "True"
                    if attempt and attempt["validation_passed"] is not None
                    else None
                ),
                validator_version=attempt["validator_version"] if attempt else None,
                handoff_ref=handoff["handoff_ref"] if handoff else None,
                established_outcome=latest_outcome["outcome"] if latest_outcome else None,
                review_cost_usd=review_cost,
                pre_flight_estimate_usd=pre_flight_estimate,
                retry_cost_overrun_usd=overrun,
                observed_cost_usd=observed_cost,
                observed_cost_complete=complete,
                outcome_revision_count=len(outcome_history),
                late_result_status=latest_late["status"] if latest_late else None,
                late_result_cost_usd=(
                    _dec(latest_late["cost_usd"]) if latest_late else None
                ),
            )
        )
    return LiveAuditableReport(rows=tuple(rows))
