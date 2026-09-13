"""Historical/shadow-mode batch analysis over already-exported attempt
and review data.

Ported from the private repo's analysis-only pilot prototype
(`pilot/invoice_exception/`, PR #2 on `inferrail-internal`, D34) --
reuses its eligibility, checkpoint-decision, and sunk-vs-incremental
cost-boundary discipline (and the bugs D35's productization found fixed
there) rather than re-deriving it. That prototype never executed
anything; this module still doesn't -- it answers "how would a policy's
recommendation have compared against what a vendor's historical export
shows actually happened," for a vendor's existing dataset. The live,
executing decision path is `engine.RecoveryEngine`, which shares this
module's enums (`models.Action`, `models.Eligibility`, etc.) but not its
data shapes -- a `WorkHistory` here can carry more than one attempt and a
review outcome; `ExceptionCase` (the live path's input) never can.

**Never fabricates a counterfactual.** A policy's recommendation is only
ever compared to reality for the subset of records where the recommended
action matches the action actually taken -- see `build_report`'s
docstring.
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .models import Action, AttemptStatus, Eligibility, ReviewOutcome


class IngestError(ValueError):
    """A raw record could not be normalized. Carries no row index of its
    own -- the caller (`ingest`) attaches that context."""


@dataclass(frozen=True)
class HistoricalAttempt:
    work_id: str
    attempt_id: str
    attempt_number: int
    status: AttemptStatus
    timestamp: datetime
    source: str
    cost_usd: Decimal | None = None
    confidence: float | None = None
    policy_version: str | None = None

    def __post_init__(self) -> None:
        if self.cost_usd is not None and self.cost_usd < 0:
            raise ValueError(f"cost_usd cannot be negative: {self.cost_usd!r}")
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1]: {self.confidence!r}")
        if self.attempt_number < 1:
            raise ValueError(f"attempt_number must be >= 1: {self.attempt_number!r}")


@dataclass(frozen=True)
class HistoricalReview:
    work_id: str
    action_taken: Action
    outcome: ReviewOutcome
    timestamp: datetime
    source: str
    correction_delta_usd: Decimal | None = None
    review_cost_usd: Decimal | None = None

    def __post_init__(self) -> None:
        if self.action_taken == Action.INSUFFICIENT_EVIDENCE:
            raise ValueError(
                "action_taken must be an action actually taken "
                "(retry or human_review), never insufficient_evidence"
            )
        for field_name, value in (
            ("correction_delta_usd", self.correction_delta_usd),
            ("review_cost_usd", self.review_cost_usd),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative: {value!r}")


@dataclass(frozen=True)
class WorkHistory:
    """Every attempt plus the current/authoritative review outcome for
    one exception, joined by `work_id`. Never mutated in place."""

    work_id: str
    attempts: tuple[HistoricalAttempt, ...]
    review: HistoricalReview | None
    review_history: tuple[HistoricalReview, ...] = ()
    """Every distinct review version, oldest first, when more than one
    was recorded (e.g. a later outcome correction). `review` is always
    the last element when non-empty -- a correction's provenance is
    never discarded in favor of "first review wins.\""""

    @property
    def known_attempt_cost_usd(self) -> Decimal | None:
        known = [a.cost_usd for a in self.attempts if a.cost_usd is not None]
        if not known:
            return None
        total = Decimal(0)
        for c in known:
            total += c
        return total


class BatchPolicy(Protocol):
    name: str

    def recommend(self, record: WorkHistory) -> BatchRecommendation: ...


@dataclass(frozen=True)
class BatchRecommendation:
    work_id: str
    policy_name: str
    action: Action
    reason: str


def _last_attempt_confidence(record: WorkHistory) -> float | None:
    if not record.attempts:
        return None
    return record.attempts[-1].confidence


@dataclass(frozen=True)
class HumanReviewBaselinePolicy:
    """Baseline 1 -- a labeled placeholder, not a real vendor's actual
    accept/reject policy: it routes every eligible record to human review
    regardless of confidence, because no real vendor's own accept-
    without-review rule is known to this module. Do not present its
    output as "what the vendor currently does.\""""

    confidence_threshold: float
    name: str = "human_review_baseline_placeholder"

    def recommend(self, record: WorkHistory) -> BatchRecommendation:
        confidence = _last_attempt_confidence(record)
        if confidence is None:
            return BatchRecommendation(
                record.work_id, self.name, Action.INSUFFICIENT_EVIDENCE,
                "no confidence value on the last attempt",
            )
        return BatchRecommendation(
            record.work_id, self.name, Action.HUMAN_REVIEW,
            f"confidence {confidence:.2f} vs. reference threshold "
            f"{self.confidence_threshold:.2f} -- placeholder baseline, not a real "
            "vendor auto-accept rule; every eligible record routes to human review",
        )


@dataclass(frozen=True)
class RetryOnceBaseline:
    """Baseline 2: always attempt one automated retry before human
    review, regardless of confidence."""

    name: str = "retry_once_baseline"

    def recommend(self, record: WorkHistory) -> BatchRecommendation:
        attempts_so_far = len(record.attempts)
        if attempts_so_far == 0:
            return BatchRecommendation(
                record.work_id, self.name, Action.INSUFFICIENT_EVIDENCE,
                "no attempts recorded yet",
            )
        if attempts_so_far == 1 and record.attempts[0].status != AttemptStatus.SUCCESS:
            return BatchRecommendation(
                record.work_id, self.name, Action.RETRY,
                "exactly one prior attempt, not yet successful -- retry once",
            )
        return BatchRecommendation(
            record.work_id, self.name, Action.HUMAN_REVIEW,
            "already retried (or succeeded) -- route to human review",
        )


@dataclass(frozen=True)
class CandidateBatchPolicy:
    """The candidate policy applied retrospectively to historical data --
    same confidence-band heuristic as `policy.PolicyConfig`'s live
    decision, restated over `WorkHistory` for comparison against a
    vendor's own recorded outcomes. Not a calibrated probability; see
    `policy.py`'s module docstring for the same caveat."""

    human_review_threshold: float
    retry_floor: float
    max_prior_attempts: int = 1
    name: str = "candidate_policy"

    def recommend(self, record: WorkHistory) -> BatchRecommendation:
        confidence = _last_attempt_confidence(record)
        if confidence is None:
            return BatchRecommendation(
                record.work_id, self.name, Action.INSUFFICIENT_EVIDENCE,
                "no confidence value on the last attempt",
            )
        if record.known_attempt_cost_usd is None and record.attempts:
            return BatchRecommendation(
                record.work_id, self.name, Action.INSUFFICIENT_EVIDENCE,
                "attempt cost unknown -- cannot weigh retry cost against review cost",
            )
        attempts_so_far = len(record.attempts)
        if attempts_so_far > self.max_prior_attempts:
            return BatchRecommendation(
                record.work_id, self.name, Action.HUMAN_REVIEW,
                f"already made {attempts_so_far} attempts -- stop retrying",
            )
        if self.retry_floor <= confidence < self.human_review_threshold:
            return BatchRecommendation(
                record.work_id, self.name, Action.RETRY,
                f"confidence {confidence:.2f} in the retry band "
                f"[{self.retry_floor:.2f}, {self.human_review_threshold:.2f})",
            )
        return BatchRecommendation(
            record.work_id, self.name, Action.HUMAN_REVIEW,
            f"confidence {confidence:.2f} outside the retry band -- route to human review",
        )


# --- Ingestion -------------------------------------------------------------


@dataclass(frozen=True)
class IngestResult:
    work_records: tuple[WorkHistory, ...]
    duplicate_attempt_ids: tuple[str, ...]
    skipped_rows: tuple[tuple[int, str], ...]
    conflicting_attempt_ids: tuple[str, ...] = ()
    duplicate_review_work_ids: tuple[str, ...] = ()
    corrected_review_work_ids: tuple[str, ...] = ()
    conflicting_review_work_ids: tuple[str, ...] = ()


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise IngestError(f"not a valid decimal: {value!r}") from exc


def _parse_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _row_to_attempt(row: dict[str, Any]) -> HistoricalAttempt:
    return HistoricalAttempt(
        work_id=str(row["work_id"]),
        attempt_id=str(row["attempt_id"]),
        attempt_number=int(row["attempt_number"]),
        status=AttemptStatus(row["status"]),
        timestamp=_parse_timestamp(row["timestamp"]),
        source=str(row.get("source", "unknown")),
        cost_usd=_parse_decimal(row.get("cost_usd")),
        confidence=_parse_optional_float(row.get("confidence")),
        policy_version=row.get("policy_version"),
    )


def _row_to_review(row: dict[str, Any]) -> HistoricalReview:
    return HistoricalReview(
        work_id=str(row["work_id"]),
        action_taken=Action(row["action_taken"]),
        outcome=ReviewOutcome(row["outcome"]),
        timestamp=_parse_timestamp(row["timestamp"]),
        source=str(row.get("source", "unknown")),
        correction_delta_usd=_parse_decimal(row.get("correction_delta_usd")),
        review_cost_usd=_parse_decimal(row.get("review_cost_usd")),
    )


def _attempt_content_key(attempt: HistoricalAttempt) -> tuple[Any, ...]:
    return (
        attempt.work_id, attempt.attempt_number, attempt.status,
        attempt.timestamp, attempt.cost_usd, attempt.confidence, attempt.policy_version,
    )


def _dedupe_identical_attempts(candidates: list[HistoricalAttempt]) -> list[HistoricalAttempt]:
    seen: set[tuple[Any, ...]] = set()
    distinct: list[HistoricalAttempt] = []
    for attempt in candidates:
        key = _attempt_content_key(attempt)
        if key in seen:
            continue
        seen.add(key)
        distinct.append(attempt)
    return distinct


def _review_content_key(review: HistoricalReview) -> tuple[Any, ...]:
    return (
        review.action_taken, review.outcome, review.timestamp,
        review.correction_delta_usd, review.review_cost_usd,
    )


def _dedupe_identical_reviews(candidates: list[HistoricalReview]) -> list[HistoricalReview]:
    seen: set[tuple[Any, ...]] = set()
    distinct: list[HistoricalReview] = []
    for review in candidates:
        key = _review_content_key(review)
        if key in seen:
            continue
        seen.add(key)
        distinct.append(review)
    return distinct


def ingest(
    attempt_rows: Iterable[dict[str, Any]],
    review_rows: Iterable[dict[str, Any]],
) -> IngestResult:
    """Normalize raw attempt/review rows into joined `WorkHistory`
    records, deduplicated by `attempt_id`/`work_id` so a repeated event
    (a replayed export, a duplicate webhook) is never counted twice --
    ported verbatim in behavior from the superseded prototype's
    `ingest.py` (see that module's docstring for the full dedup/conflict
    rules this preserves)."""
    skipped: list[tuple[int, str]] = []
    raw_attempts_by_id: dict[str, list[HistoricalAttempt]] = defaultdict(list)

    for i, row in enumerate(attempt_rows):
        try:
            attempt = _row_to_attempt(row)
        except (IngestError, KeyError, ValueError) as exc:
            skipped.append((i, f"attempt row: {exc}"))
            continue
        raw_attempts_by_id[attempt.attempt_id].append(attempt)

    duplicate_ids: list[str] = []
    conflicting_attempt_ids: list[str] = []
    attempts_by_work: dict[str, list[HistoricalAttempt]] = defaultdict(list)

    for attempt_id, attempt_candidates in raw_attempts_by_id.items():
        if len(attempt_candidates) == 1:
            attempt = attempt_candidates[0]
            attempts_by_work[attempt.work_id].append(attempt)
            continue

        distinct_attempts = _dedupe_identical_attempts(attempt_candidates)
        if len(distinct_attempts) < len(attempt_candidates):
            duplicate_ids.append(attempt_id)
        if len(distinct_attempts) == 1:
            attempt = distinct_attempts[0]
            attempts_by_work[attempt.work_id].append(attempt)
            continue

        conflicting_attempt_ids.append(attempt_id)
        skipped.append((
            -1,
            f"conflicting attempt rows for attempt_id {attempt_id!r} -- "
            "same ID, disagreeing content, none treated as authoritative",
        ))

    raw_reviews_by_work: dict[str, list[HistoricalReview]] = defaultdict(list)
    for i, row in enumerate(review_rows):
        try:
            review = _row_to_review(row)
        except (IngestError, KeyError, ValueError) as exc:
            skipped.append((i, f"review row: {exc}"))
            continue
        raw_reviews_by_work[review.work_id].append(review)

    reviews_by_work: dict[str, HistoricalReview] = {}
    review_history_by_work: dict[str, tuple[HistoricalReview, ...]] = {}
    duplicate_review_work_ids: list[str] = []
    corrected_review_work_ids: list[str] = []
    conflicting_review_work_ids: list[str] = []

    for work_id, candidates in raw_reviews_by_work.items():
        if len(candidates) == 1:
            reviews_by_work[work_id] = candidates[0]
            continue

        distinct = _dedupe_identical_reviews(candidates)
        if len(distinct) < len(candidates):
            duplicate_review_work_ids.append(work_id)
        if len(distinct) == 1:
            reviews_by_work[work_id] = distinct[0]
            continue

        ordered = sorted(distinct, key=lambda r: r.timestamp)
        if ordered[-1].timestamp == ordered[-2].timestamp:
            conflicting_review_work_ids.append(work_id)
            skipped.append((
                -1,
                f"conflicting reviews for work_id {work_id!r} with indistinguishable "
                "timestamps -- neither treated as authoritative",
            ))
            continue
        reviews_by_work[work_id] = ordered[-1]
        review_history_by_work[work_id] = tuple(ordered)
        corrected_review_work_ids.append(work_id)

    all_work_ids = set(attempts_by_work) | set(raw_reviews_by_work)
    work_records = tuple(
        WorkHistory(
            work_id=work_id,
            attempts=tuple(
                sorted(attempts_by_work.get(work_id, ()), key=lambda a: a.attempt_number)
            ),
            review=reviews_by_work.get(work_id),
            review_history=review_history_by_work.get(work_id, ()),
        )
        for work_id in sorted(all_work_ids)
    )

    return IngestResult(
        work_records=work_records,
        duplicate_attempt_ids=tuple(sorted(duplicate_ids)),
        skipped_rows=tuple(skipped),
        conflicting_attempt_ids=tuple(sorted(conflicting_attempt_ids)),
        duplicate_review_work_ids=tuple(sorted(duplicate_review_work_ids)),
        corrected_review_work_ids=tuple(sorted(corrected_review_work_ids)),
        conflicting_review_work_ids=tuple(sorted(conflicting_review_work_ids)),
    )


# --- Auditable report --------------------------------------------------


@dataclass(frozen=True)
class PolicyRowResult:
    work_id: str
    policy_name: str
    recommended_action: Action
    reason: str
    actual_action_taken: Action | None
    agrees_with_actual: bool | None
    observed_outcome: ReviewOutcome | None
    sunk_cost_usd: Decimal | None
    observed_cost_usd: Decimal | None
    observed_cost_complete: bool


@dataclass(frozen=True)
class PolicySummary:
    policy_name: str
    total_records: int
    insufficient_evidence_count: int
    comparable_count: int
    agreement_count: int
    agreement_rate: float | None
    known_cost_total_usd: Decimal | None
    known_cost_count: int
    known_cost_all_complete: bool


@dataclass(frozen=True)
class ExcludedRecord:
    work_id: str
    status: Eligibility
    reason: str


@dataclass(frozen=True)
class AuditableReport:
    rows: tuple[PolicyRowResult, ...]
    summaries: tuple[PolicySummary, ...]
    total_input_records: int
    skipped_rows: tuple[tuple[int, str], ...]
    duplicate_attempt_ids: tuple[str, ...]
    excluded_records: tuple[ExcludedRecord, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_input_records": self.total_input_records,
            "duplicate_attempt_ids": list(self.duplicate_attempt_ids),
            "skipped_rows": [
                {"row_index": i, "reason": reason} for i, reason in self.skipped_rows
            ],
            "summaries": [
                {
                    "policy_name": s.policy_name,
                    "total_records": s.total_records,
                    "insufficient_evidence_count": s.insufficient_evidence_count,
                    "comparable_count": s.comparable_count,
                    "agreement_count": s.agreement_count,
                    "agreement_rate": s.agreement_rate,
                    "known_cost_total_usd": (
                        str(s.known_cost_total_usd) if s.known_cost_total_usd is not None else None
                    ),
                    "known_cost_count": s.known_cost_count,
                    "known_cost_all_complete": s.known_cost_all_complete,
                }
                for s in self.summaries
            ],
            "rows": [
                {
                    "work_id": r.work_id,
                    "policy_name": r.policy_name,
                    "recommended_action": r.recommended_action.value,
                    "reason": r.reason,
                    "actual_action_taken": (
                        r.actual_action_taken.value if r.actual_action_taken else None
                    ),
                    "agrees_with_actual": r.agrees_with_actual,
                    "observed_outcome": (
                        r.observed_outcome.value if r.observed_outcome else None
                    ),
                    "sunk_cost_usd": str(r.sunk_cost_usd) if r.sunk_cost_usd is not None else None,
                    "observed_cost_usd": (
                        str(r.observed_cost_usd) if r.observed_cost_usd is not None else None
                    ),
                    "observed_cost_complete": r.observed_cost_complete,
                }
                for r in self.rows
            ],
            "excluded_records": [
                {"work_id": e.work_id, "status": e.status.value, "reason": e.reason}
                for e in self.excluded_records
            ],
        }


def classify_eligibility(record: WorkHistory) -> tuple[Eligibility, str | None]:
    if not record.attempts:
        return Eligibility.ELIGIBLE, None

    checkpoint = record.attempts[0]
    if checkpoint.status == AttemptStatus.SUCCESS:
        return (
            Eligibility.ALREADY_RESOLVED,
            "checkpoint (first) attempt already succeeded -- no exception to route",
        )

    if len(record.attempts) > 2:
        return (
            Eligibility.UNSUPPORTED_SEQUENCE,
            f"{len(record.attempts)} attempts recorded -- this release's single-shot "
            "decision model supports at most one retry after the checkpoint attempt",
        )

    if len(record.attempts) == 2:
        second = record.attempts[1]
        if second.timestamp <= checkpoint.timestamp:
            return (
                Eligibility.UNSUPPORTED_SEQUENCE,
                "second attempt is not timestamped after the checkpoint attempt",
            )
        if record.review is not None and record.review.timestamp < second.timestamp:
            return (
                Eligibility.UNSUPPORTED_SEQUENCE,
                "review is timestamped before a later attempt -- the review, not a "
                "retry, was the checkpoint's actual next event",
            )

    return Eligibility.ELIGIBLE, None


def _checkpoint_view(record: WorkHistory) -> WorkHistory:
    if not record.attempts:
        return record
    return dataclasses.replace(
        record, attempts=record.attempts[:1], review=None, review_history=()
    )


def _actual_action_at_checkpoint(record: WorkHistory) -> Action | None:
    if len(record.attempts) > 1:
        return Action.RETRY
    if record.review is not None:
        return record.review.action_taken
    return None


def _decision_cost(record: WorkHistory, actual_action: Action) -> tuple[Decimal | None, bool]:
    components: list[Decimal] = []
    complete = True

    if actual_action == Action.RETRY:
        for attempt in record.attempts[1:]:
            if attempt.cost_usd is None:
                complete = False
            else:
                components.append(attempt.cost_usd)

        retry_succeeded = record.attempts[-1].status == AttemptStatus.SUCCESS
        if not retry_succeeded and record.review is None:
            complete = False

    if record.review is not None and record.review.action_taken == Action.HUMAN_REVIEW:
        if record.review.review_cost_usd is None:
            complete = False
        else:
            components.append(record.review.review_cost_usd)

    if not components:
        return (None, complete)
    total = Decimal(0)
    for c in components:
        total += c
    return (total, complete)


def _evaluate_one(record: WorkHistory, recommendation: BatchRecommendation) -> PolicyRowResult:
    actual_action = _actual_action_at_checkpoint(record)
    sunk_cost = record.attempts[0].cost_usd if record.attempts else None

    if recommendation.action == Action.INSUFFICIENT_EVIDENCE:
        return PolicyRowResult(
            work_id=record.work_id, policy_name=recommendation.policy_name,
            recommended_action=recommendation.action, reason=recommendation.reason,
            actual_action_taken=actual_action, agrees_with_actual=None,
            observed_outcome=None, sunk_cost_usd=sunk_cost,
            observed_cost_usd=None, observed_cost_complete=False,
        )

    if actual_action is None:
        return PolicyRowResult(
            work_id=record.work_id, policy_name=recommendation.policy_name,
            recommended_action=recommendation.action, reason=recommendation.reason,
            actual_action_taken=None, agrees_with_actual=None,
            observed_outcome=None, sunk_cost_usd=sunk_cost,
            observed_cost_usd=None, observed_cost_complete=False,
        )

    agrees = recommendation.action == actual_action
    if not agrees:
        return PolicyRowResult(
            work_id=record.work_id, policy_name=recommendation.policy_name,
            recommended_action=recommendation.action, reason=recommendation.reason,
            actual_action_taken=actual_action, agrees_with_actual=False,
            observed_outcome=None, sunk_cost_usd=sunk_cost,
            observed_cost_usd=None, observed_cost_complete=False,
        )

    observed_cost, cost_complete = _decision_cost(record, actual_action)
    observed_outcome = record.review.outcome if record.review is not None else None
    return PolicyRowResult(
        work_id=record.work_id, policy_name=recommendation.policy_name,
        recommended_action=recommendation.action, reason=recommendation.reason,
        actual_action_taken=actual_action, agrees_with_actual=True,
        observed_outcome=observed_outcome, sunk_cost_usd=sunk_cost,
        observed_cost_usd=observed_cost, observed_cost_complete=cost_complete,
    )


def _summarize(policy_name: str, rows: list[PolicyRowResult]) -> PolicySummary:
    total = len(rows)
    insufficient = sum(1 for r in rows if r.recommended_action == Action.INSUFFICIENT_EVIDENCE)
    comparable_rows = [r for r in rows if r.agrees_with_actual is not None]
    comparable = len(comparable_rows)
    agreements = [r for r in comparable_rows if r.agrees_with_actual]
    agreement_count = len(agreements)
    agreement_rate = (agreement_count / comparable) if comparable > 0 else None

    known_costs = [r.observed_cost_usd for r in agreements if r.observed_cost_usd is not None]
    known_cost_total: Decimal | None = None
    if known_costs:
        known_cost_total = Decimal(0)
        for c in known_costs:
            known_cost_total += c
    known_cost_all_complete = (
        all(r.observed_cost_complete for r in agreements) and len(known_costs) == len(agreements)
    )

    return PolicySummary(
        policy_name=policy_name, total_records=total,
        insufficient_evidence_count=insufficient, comparable_count=comparable,
        agreement_count=agreement_count, agreement_rate=agreement_rate,
        known_cost_total_usd=known_cost_total, known_cost_count=len(known_costs),
        known_cost_all_complete=known_cost_all_complete,
    )


def build_report(
    records: tuple[WorkHistory, ...],
    policies: tuple[BatchPolicy, ...],
    *,
    total_input_records: int,
    skipped_rows: tuple[tuple[int, str], ...],
    duplicate_attempt_ids: tuple[str, ...],
) -> AuditableReport:
    """Run every policy over every eligible record in shadow mode -- no
    recommendation here is ever acted on; this module only ever compares
    against a vendor's already-recorded history. See the module
    docstring's "Never fabricates a counterfactual.\""""
    eligible_records: list[WorkHistory] = []
    excluded: list[ExcludedRecord] = []
    for record in records:
        status, reason = classify_eligibility(record)
        if status == Eligibility.ELIGIBLE:
            eligible_records.append(record)
        else:
            assert reason is not None
            excluded.append(ExcludedRecord(work_id=record.work_id, status=status, reason=reason))

    all_rows: list[PolicyRowResult] = []
    summaries: list[PolicySummary] = []

    for policy in policies:
        policy_rows = [
            _evaluate_one(record, policy.recommend(_checkpoint_view(record)))
            for record in eligible_records
        ]
        all_rows.extend(policy_rows)
        summaries.append(_summarize(policy.name, policy_rows))

    return AuditableReport(
        rows=tuple(all_rows),
        summaries=tuple(summaries),
        total_input_records=total_input_records,
        skipped_rows=skipped_rows,
        duplicate_attempt_ids=duplicate_attempt_ids,
        excluded_records=tuple(excluded),
    )


__all__ = [
    "AuditableReport", "BatchPolicy", "BatchRecommendation", "CandidateBatchPolicy",
    "ExcludedRecord", "HistoricalAttempt", "HistoricalReview", "HumanReviewBaselinePolicy",
    "IngestError", "IngestResult", "PolicyRowResult", "PolicySummary", "RetryOnceBaseline",
    "WorkHistory", "build_report", "classify_eligibility", "ingest",
]
