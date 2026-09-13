"""How a vendor integrates `inferrail.ap` into their own pipeline:
implement your own `RetryAdapter` and `HumanReviewHandoff`, wire them
into a `RecoveryEngine`, and call `decide()` for each eligible exception.

This example's adapter and handoff are still local stand-ins (no real
extraction system, no real review queue) -- swap `MyExtractionRetryAdapter`
for a call into your own extraction pipeline, and `MyReviewQueueHandoff`
for your own queue/ticketing API. Nothing here calls a network or an
Inferrail-operated service; that is the point (see docs/capabilities/
ap-invoice-exception-recovery.md's "Data boundary").

Threads the **full chain** end to end, in one coherent run: exception
input -> decision -> permitted retry -> validation -> usable recovered
result (real field values, not just a status) **or** an acknowledged
human-review handoff -> recorded outcome -> accurate report. A success
status alone is not the point -- the recovered invoice fields
themselves are what a real integration needs back.

Usage:
    python examples/ap_invoice_exception_recovery/custom_integration_example.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from inferrail.ap import (
    Action,
    AttemptStatus,
    CostEstimate,
    ExceptionCase,
    FailureType,
    PolicyConfig,
    Recommendation,
    RecoveryEngine,
    RecoveryStore,
    RetryAttemptResult,
)
from inferrail.ap.report import build_live_report
from inferrail.ap.validation import FieldPresenceAndConfidenceValidator


@dataclass
class MyExtractionRetryAdapter:
    """Stand-in for a real vendor's own re-extraction call. Replace the
    body of `retry` with whatever actually re-runs extraction for one
    invoice (an internal OCR service, a licensed extraction engine,
    another LLM call) -- the engine reads back a `RetryAttemptResult`,
    including `raw_fields` (the re-extracted values themselves), never
    invoice content beyond what you put there.

    `estimate_cost` is required for a retry to be authorized at all
    (`policy.authorize_retry_cost`) -- an adapter that cannot bound its
    own next call's cost is treated the same as one whose bound exceeds
    policy: never invoked, routed to human review instead."""

    name: str = "my_extraction_service"

    def retry(self, case: ExceptionCase) -> RetryAttemptResult:
        # Real integration: call your own extraction pipeline here using
        # `case.work_id` to look up the invoice in *your* system. This
        # example returns canned, but usable, re-extracted field values.
        return RetryAttemptResult(
            attempt_id=f"{case.work_id}-retry-1",
            status=AttemptStatus.SUCCESS,
            cost_usd=Decimal("0.08"),
            confidence=0.96,
            provider=self.name,
            raw_fields={
                "invoice_number": "90210",
                "vendor": "Acme Corp",
                "total": "1200.00",
            },
        )

    def estimate_cost(self, case: ExceptionCase) -> CostEstimate:
        # Real integration: whatever your own extraction pipeline can
        # defensibly bound its next call at (a per-page rate, a fixed
        # per-call ceiling, a provider's own quote) -- never a hard
        # billing guarantee, see docs/capabilities/
        # ap-invoice-exception-recovery.md's "Prospective retry-cost
        # authorization."
        return CostEstimate(amount_usd=Decimal("0.10"), basis="my_extraction_service flat rate")


@dataclass
class MyReviewQueueHandoff:
    """Stand-in for a real review-queue integration (a ticketing API, an
    internal ops tool). Replace `send` with the actual API call; return
    whatever reference id your system gives back."""

    def send(
        self,
        case: ExceptionCase,
        recommendation: Recommendation,
        attempt_history: tuple[dict[str, Any], ...],
    ) -> str:
        print(
            f"[MyReviewQueueHandoff] would enqueue work_id={case.work_id!r} "
            f"reason={recommendation.reason!r} history={attempt_history}"
        )
        return f"my-queue-ticket-{case.work_id}"


def _config() -> PolicyConfig:
    return PolicyConfig(
        eligible_failure_types=frozenset(
            {FailureType.LOW_CONFIDENCE.value, FailureType.VALIDATION_CHECK_FAILED.value}
        ),
        retry_floor=0.5,
        human_review_threshold=0.75,
        max_retry_cost_usd=Decimal("1.00"),
        decision_deadline_seconds=86400.0,
    )


def main() -> None:
    store = RecoveryStore(Path("./inferrail-ap-integration-example.sqlite3"))
    engine = RecoveryEngine(
        store=store,
        config=_config(),
        retry_adapter=MyExtractionRetryAdapter(),
        validator=FieldPresenceAndConfidenceValidator(min_confidence=0.9),
        handoff=MyReviewQueueHandoff(),
    )

    print("=== Scenario 1: retry recovers the exception -- usable data comes back ===")
    recovered_case = ExceptionCase(
        work_id="INV-90210",
        checkpoint_attempt_id="INV-90210-checkpoint",
        failure_type=FailureType.LOW_CONFIDENCE.value,
        confidence=0.61,
        cost_so_far_usd=Decimal("0.15"),
        opened_at=datetime.now(UTC),
        source="my_extraction_pipeline",
    )
    result = engine.decide(recovered_case)
    print(f"decision={result.decision_id} action={result.recommended_action.value} "
          f"status={result.status}")

    if result.status == "retry_resolved":
        assert result.raw_fields, "expected usable recovered fields, not just a status"
        print(f"recovered fields (usable data, not just a status): {result.raw_fields}")
    elif result.handoff_ref:
        print(f"handed off to your review queue as {result.handoff_ref!r}")

    print("\n=== Scenario 2: policy routes straight to human review -- full resolution ===")
    review_case = ExceptionCase(
        work_id="INV-55501",
        checkpoint_attempt_id="INV-55501-checkpoint",
        failure_type=FailureType.LOW_CONFIDENCE.value,
        confidence=0.95,  # outside the retry band -> human_review, no adapter call
        cost_so_far_usd=Decimal("0.15"),
        opened_at=datetime.now(UTC),
        source="my_extraction_pipeline",
    )
    review_result = engine.decide(review_case)
    assert review_result.recommended_action == Action.HUMAN_REVIEW
    assert review_result.handoff_ref is not None
    print(f"handed off to your review queue as {review_result.handoff_ref!r}")

    # Your review queue eventually resolves it -- record the real outcome.
    engine.record_outcome(
        work_id="INV-55501",
        outcome="corrected",
        timestamp=datetime.now(UTC).timestamp(),
        source="my_review_queue",
        correction_delta_usd=Decimal("42.00"),
        review_cost_usd=Decimal("3.50"),
    )
    print("recorded the review queue's real outcome for INV-55501")

    print("\n=== Inspecting the joined, auditable report for both work_ids ===")
    report = build_live_report(store)
    print(json.dumps(report.to_dict(), indent=2, default=str))


if __name__ == "__main__":
    main()
