"""LIVE-PROVIDER EXECUTION -- makes real, billed OpenAI API calls.

Unlike `inferrail ap demo` (fixture-based, deterministic, zero-key), this
script runs the real `inferrail.ap.engine.RecoveryEngine` against
`OpenAIRetryAdapter`, which sends invoice text directly from this process
to OpenAI's API for one real re-extraction call per eligible case. It
never sends invoice content to any Inferrail-operated service -- see
docs/capabilities/ap-invoice-exception-recovery.md's "Data boundary".

The invoice text below is synthetic (written for this example), not a
real customer's data -- but the OpenAI call itself is real and will be
billed to whatever account owns OPENAI_API_KEY.

Usage:
    OPENAI_API_KEY=sk-... python examples/ap_invoice_exception_recovery/live_openai_demo.py

Requires: pip install "inferrail[ap]"
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from inferrail.ap import (
    ExceptionCase,
    FailureType,
    FieldPresenceAndConfidenceValidator,
    LoggingHandoff,
    OpenAIRetryAdapter,
    PolicyConfig,
    RecoveryEngine,
    RecoveryStore,
    authorize_retry_cost,
)

INVOICE_TEXT = (
    "INVOICE #7734\n"
    "Vendor: Northwind Traders\n"
    "Date: 2026-05-14\n"
    "Line items:\n"
    "  Office chairs x4 @ $145.00 = $580.00\n"
    "  Standing desks x2 @ $310.00 = $620.00\n"
    "Total: $1,200.00\n"
)


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "error: OPENAI_API_KEY is not set. This example makes a real, billed "
            "OpenAI call -- see 'inferrail ap demo' for a zero-key, fixture-based "
            "walkthrough instead.",
            file=sys.stderr,
        )
        return 1

    store = RecoveryStore(Path("./inferrail-ap-live-demo.sqlite3"))
    handoff = LoggingHandoff(path=Path("./inferrail-ap-live-demo-handoffs.jsonl"))
    config = PolicyConfig(
        eligible_failure_types=frozenset({FailureType.LOW_CONFIDENCE.value}),
        retry_floor=0.4,
        human_review_threshold=0.8,
        max_retry_cost_usd=Decimal("0.05"),
        decision_deadline_seconds=86400.0,
    )
    adapter = OpenAIRetryAdapter(
        invoice_text_by_work_id={"LIVE-7734": INVOICE_TEXT},
        required_fields=("invoice_number", "vendor", "total"),
    )
    engine = RecoveryEngine(
        store=store,
        config=config,
        retry_adapter=adapter,
        validator=FieldPresenceAndConfidenceValidator(min_confidence=0.7),
        handoff=handoff,
    )

    case = ExceptionCase(
        work_id="LIVE-7734",
        checkpoint_attempt_id="LIVE-7734-checkpoint",
        failure_type=FailureType.LOW_CONFIDENCE.value,
        confidence=0.55,  # the original extraction's own (lower) confidence
        cost_so_far_usd=Decimal("0.01"),
        opened_at=datetime.now(UTC),
        source="live_openai_demo_example",
    )

    # Surfaced explicitly, before the real call: the same pre-flight
    # authorization RecoveryEngine._execute_retry performs internally --
    # see docs/capabilities/ap-invoice-exception-recovery.md's
    # "Prospective retry-cost authorization." A defensible upper bound,
    # never a hard OpenAI billing guarantee.
    estimate = adapter.estimate_cost(case)
    authorized, reason = authorize_retry_cost(
        estimate=estimate, max_retry_cost_usd=config.max_retry_cost_usd
    )
    print(f"pre-flight cost authorization: authorized={authorized} ({reason})")
    if not authorized:
        print("not authorized -- the engine will route to human review without calling OpenAI.")

    print("\nMaking one real OpenAI call to re-extract this invoice's fields...")
    result = engine.decide(case)
    print(f"\ndecision_id={result.decision_id}")
    print(f"recommended_action={result.recommended_action.value}")
    print(f"status={result.status}")
    print(f"retry_status={result.retry_status.value if result.retry_status else None}")
    print(f"retry_cost_usd={result.retry_cost_usd}")
    if result.status == "retry_resolved":
        print(f"recovered fields (usable data, not just a status): {result.raw_fields}")
        # Tolerant to real model variance -- checks the known invoice
        # number actually made it back, not an exact-match on every
        # field (a live model call is not deterministic).
        assert result.raw_fields.get("invoice_number", "").strip("# ") == "7734", (
            f"expected the real invoice_number back from the live call, got {result.raw_fields!r}"
        )
    if result.handoff_ref:
        print(f"handed off to human review: {result.handoff_ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
