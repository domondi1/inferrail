"""How a vendor integrates `inferrail.ap` into their own pipeline:
implement your own `RetryAdapter` and `HumanReviewHandoff`, wire them
into a `RecoveryEngine`, and call `decide()` for each eligible exception.

This example's adapter and handoff are still local stand-ins (no real
extraction system, no real review queue) -- swap `MyExtractionRetryAdapter`
for a call into your own extraction pipeline, and `MyReviewQueueHandoff`
for your own queue/ticketing API. Nothing here calls a network or an
Inferrail-operated service; that is the point (see docs/capabilities/
ap-invoice-exception-recovery.md's "Data boundary").

Usage:
    python examples/ap_invoice_exception_recovery/custom_integration_example.py
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from inferrail.ap import (
    Action,
    AttemptStatus,
    ExceptionCase,
    FailureType,
    PolicyConfig,
    RecoveryEngine,
    RecoveryStore,
    Recommendation,
    RetryAttemptResult,
)
from inferrail.ap.validation import FieldPresenceAndConfidenceValidator


@dataclass
class MyExtractionRetryAdapter:
    """Stand-in for a real vendor's own re-extraction call. Replace the
    body of `retry` with whatever actually re-runs extraction for one
    invoice (an internal OCR service, a licensed extraction engine,
    another LLM call) -- the engine only needs a `RetryAttemptResult`
    back, never invoice content itself."""

    name: str = "my_extraction_service"

    def retry(self, case: ExceptionCase) -> RetryAttemptResult:
        # Real integration: call your own extraction pipeline here using
        # `case.work_id` to look up the invoice in *your* system -- this
        # example just returns a canned "it worked" result.
        return RetryAttemptResult(
            attempt_id=f"{case.work_id}-retry-1",
            status=AttemptStatus.SUCCESS,
            cost_usd=Decimal("0.08"),
            confidence=0.96,
            provider=self.name,
        )


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


def main() -> None:
    store = RecoveryStore(Path("./inferrail-ap-integration-example.sqlite3"))
    config = PolicyConfig(
        eligible_failure_types=frozenset(
            {FailureType.LOW_CONFIDENCE.value, FailureType.VALIDATION_CHECK_FAILED.value}
        ),
        retry_floor=0.5,
        human_review_threshold=0.75,
        max_retry_cost_usd=Decimal("1.00"),
        decision_deadline_seconds=86400.0,
    )
    engine = RecoveryEngine(
        store=store,
        config=config,
        retry_adapter=MyExtractionRetryAdapter(),
        validator=FieldPresenceAndConfidenceValidator(min_confidence=0.9),
        handoff=MyReviewQueueHandoff(),
    )

    case = ExceptionCase(
        work_id="INV-90210",
        checkpoint_attempt_id="INV-90210-checkpoint",
        failure_type=FailureType.LOW_CONFIDENCE.value,
        confidence=0.61,
        cost_so_far_usd=Decimal("0.15"),
        opened_at=datetime.now(UTC),
        source="my_extraction_pipeline",
    )
    result = engine.decide(case)
    print(f"decision={result.decision_id} action={result.recommended_action.value} "
          f"status={result.status}")

    if result.recommended_action == Action.HUMAN_REVIEW and result.handoff_ref:
        print(f"handed off to your review queue as {result.handoff_ref!r}")
    elif result.status == "retry_resolved":
        print("resolved by the one permitted retry -- no human review needed")


if __name__ == "__main__":
    main()
