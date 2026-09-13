"""`inferrail ap`: AP invoice-exception recovery commands.

- `inferrail ap demo` -- DEMO DATA, fixture-based, zero-key, deterministic.
  Runs the exact same `inferrail.ap.engine.RecoveryEngine` code path a real
  integration uses, against canned `FixtureRetryAdapter` results, so the
  five core scenarios (retry succeeds, retry fails -> human review, policy
  disallows retry, a repeated request handled idempotently, and
  decision/outcome inspection) are visible in a few seconds with no API
  key, no network access, and no money spent.
- `inferrail ap report` -- print the auditable report for a given store.
- `inferrail ap outcome` -- record a real human-review outcome.
- `inferrail ap reap` -- operator recovery: finds every decision whose
  retry lease has expired (its worker is presumed dead -- see
  `inferrail.ap.engine.RecoveryEngine.reap_stale_retries`) and moves each
  to `awaiting_human_review`, never re-invoking the customer's retry
  adapter. Safe to run on a schedule.
- `inferrail ap batch` -- historical/shadow-mode analysis over an already-
  exported vendor dataset (see `inferrail.ap.batch`); never executes
  anything.

See docs/capabilities/ap-invoice-exception-recovery.md for the full
walkthrough and `examples/ap_invoice_exception_recovery/` for a scripted,
narrated version of the same five scenarios.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from inferrail.ap.adapters import FixtureRetryAdapter, RetryAdapter
from inferrail.ap.batch import (
    BatchPolicy,
    CandidateBatchPolicy,
    HumanReviewBaselinePolicy,
    RetryOnceBaseline,
    build_report,
    ingest,
)
from inferrail.ap.engine import RecoveryEngine
from inferrail.ap.handoff import LoggingHandoff
from inferrail.ap.models import AttemptStatus, ExceptionCase, FailureType, RetryAttemptResult
from inferrail.ap.policy import PolicyConfig
from inferrail.ap.report import build_live_report
from inferrail.ap.store import RecoveryStore
from inferrail.ap.validation import FieldPresenceAndConfidenceValidator

DEMO_DB_PATH = Path("./inferrail-ap-demo.sqlite3")
DEMO_HANDOFF_LOG_PATH = Path("./inferrail-ap-demo-handoffs.jsonl")

_DEMO_CONFIG = PolicyConfig(
    eligible_failure_types=frozenset(
        {FailureType.LOW_CONFIDENCE.value, FailureType.VALIDATION_CHECK_FAILED.value}
    ),
    retry_floor=0.5,
    human_review_threshold=0.75,
    max_retry_cost_usd=Decimal("1.00"),
    decision_deadline_seconds=86400.0,
)


def _demo_case(work_id: str, *, confidence: float, opened_at: datetime) -> ExceptionCase:
    return ExceptionCase(
        work_id=work_id,
        checkpoint_attempt_id=f"{work_id}-checkpoint",
        failure_type=FailureType.LOW_CONFIDENCE.value,
        confidence=confidence,
        cost_so_far_usd=Decimal("0.12"),
        opened_at=opened_at,
        source="demo_fixture",
    )


def run_ap_demo() -> int:
    if DEMO_DB_PATH.exists():
        DEMO_DB_PATH.unlink()  # fresh, reproducible output on every run
    if DEMO_HANDOFF_LOG_PATH.exists():
        DEMO_HANDOFF_LOG_PATH.unlink()

    print("=" * 72)
    print("INFERRAIL AP DEMO -- fixture-based, deterministic, no API key required")
    print("=" * 72)
    print(f"Decision/attempt/outcome store: {DEMO_DB_PATH}")
    print(f"Human-review handoff log:       {DEMO_HANDOFF_LOG_PATH}\n")

    now = datetime.now(UTC)
    store = RecoveryStore(DEMO_DB_PATH)
    handoff = LoggingHandoff(path=DEMO_HANDOFF_LOG_PATH)
    fixture_adapter = FixtureRetryAdapter(
        results_by_work_id={
            "demo-retry-succeeds": RetryAttemptResult(
                attempt_id="demo-retry-succeeds-retry",
                status=AttemptStatus.SUCCESS,
                cost_usd=Decimal("0.06"),
                confidence=0.97,
                provider="fixture",
                raw_fields={
                    "invoice_number": "INV-10482",
                    "vendor": "Acme Supply Co",
                    "total": "1420.00",
                },
            ),
            "demo-retry-fails": RetryAttemptResult(
                attempt_id="demo-retry-fails-retry",
                status=AttemptStatus.FAILED,
                cost_usd=Decimal("0.06"),
                confidence=0.35,
                provider="fixture",
            ),
        },
        estimated_costs_by_work_id={
            "demo-retry-succeeds": Decimal("0.06"),
            "demo-retry-fails": Decimal("0.06"),
        },
    )
    engine = RecoveryEngine(
        store=store,
        config=_DEMO_CONFIG,
        retry_adapter=cast("RetryAdapter", fixture_adapter),
        validator=FieldPresenceAndConfidenceValidator(min_confidence=0.9),
        handoff=handoff,
    )

    print("1. Eligible exception, one retry recovers it:")
    case = _demo_case("demo-retry-succeeds", confidence=0.60, opened_at=now)
    result = engine.decide(case, now=now)
    print(f"   recommended={result.recommended_action.value} status={result.status} "
          f"retry_status={result.retry_status.value if result.retry_status else None}")
    print(f"   recovered fields (usable data, not just a status): {result.raw_fields}")

    print("\n2. Eligible exception, retry does not resolve it -> established human review:")
    case = _demo_case("demo-retry-fails", confidence=0.60, opened_at=now)
    result = engine.decide(case, now=now)
    print(f"   recommended={result.recommended_action.value} status={result.status} "
          f"retry_status={result.retry_status.value if result.retry_status else None} "
          f"handoff_ref={result.handoff_ref}")

    print("\n3. Policy disallows retry (confidence already high) -> straight to human review,")
    print("   no retry adapter ever invoked:")
    case = _demo_case("demo-policy-disallows-retry", confidence=0.95, opened_at=now)
    result = engine.decide(case, now=now)
    print(f"   recommended={result.recommended_action.value} status={result.status} "
          f"reason={result.reason!r}")

    print("\n4. Repeated request for the same work_id is handled idempotently --")
    print("   the recorded decision is returned, nothing is re-executed:")
    replay = engine.decide(case, now=now)
    print(f"   idempotent_replay={replay.idempotent_replay} "
          f"decision_id matches original: {replay.decision_id == result.decision_id}")

    print("\n5. Recording a real human-review outcome for the routed case, then")
    print("   inspecting the joined decision/attempt/outcome report:")
    engine.record_outcome(
        work_id="demo-policy-disallows-retry",
        outcome="corrected",
        timestamp=now.timestamp(),
        source="demo_review_queue",
        correction_delta_usd=Decimal("9.40"),
        review_cost_usd=Decimal("2.50"),
    )
    report = build_live_report(store)
    print(json.dumps(report.to_dict(), indent=2, default=str))

    print("\n" + "-" * 72)
    print(f"Inspect the raw store yourself: sqlite3 {DEMO_DB_PATH} 'select * from decisions;'")
    print(f"Inspect the handoff log: cat {DEMO_HANDOFF_LOG_PATH}")
    print("Run 'inferrail ap report --db "
          f"{DEMO_DB_PATH}' any time to reprint this report.")
    print("\nEvery number above is fixture data (see FixtureRetryAdapter) -- to run the")
    print("same engine against a real OpenAI call instead, see")
    print("examples/ap_invoice_exception_recovery/live_openai_demo.py (requires")
    print("OPENAI_API_KEY; makes one real, billed call per case).")
    return 0


def run_ap_report(db_path: Path, *, as_json: bool) -> int:
    if not db_path.exists():
        print(f"error: no store found at {db_path}", file=sys.stderr)
        return 1
    report = build_live_report(RecoveryStore(db_path))
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
        return 0
    for row in report.rows:
        print(
            f"{row.work_id:20} decision={row.decision_id} "
            f"action={row.recommended_action:14} status={row.status:20} "
            f"retry_status={row.retry_status or '-':10} "
            f"validation_passed={row.validation_passed} "
            f"established_outcome={row.established_outcome}"
        )
    return 0


def run_ap_outcome(
    db_path: Path,
    work_id: str,
    outcome: str,
    *,
    correction_delta_usd: str | None,
    review_cost_usd: str | None,
    source: str,
) -> int:
    store = RecoveryStore(db_path)
    try:
        store.record_outcome(
            work_id=work_id,
            outcome=outcome,
            timestamp=datetime.now(UTC).timestamp(),
            source=source,
            correction_delta_usd=correction_delta_usd,
            review_cost_usd=review_cost_usd,
        )
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"recorded outcome={outcome!r} for work_id={work_id!r}")
    return 0


def run_ap_reap(db_path: Path, *, as_json: bool) -> int:
    """Operator recovery: reaps every decision whose retry lease has
    expired. Talks to the store directly (like `run_ap_outcome`) rather
    than constructing a full `RecoveryEngine` -- reaping is a pure
    store-level status transition that never invokes a retry adapter or
    sends a handoff (see `inferrail.ap.store.RecoveryStore.
    reap_stale_retry_lease`); a caller wanting a handoff sent for a
    reaped work_id uses `RecoveryEngine.ensure_handoff` with its own
    re-supplied `ExceptionCase`."""
    if not db_path.exists():
        print(f"error: no store found at {db_path}", file=sys.stderr)
        return 1
    store = RecoveryStore(db_path)
    reaped: list[dict[str, str]] = []
    for row in store.find_stale_retry_leases():
        result = store.reap_stale_retry_lease(row["work_id"])
        if result is not None:
            reaped.append({"work_id": row["work_id"], "reaped_attempt_id": result["attempt_id"]})
    if as_json:
        print(json.dumps({"reaped": reaped}, indent=2))
    else:
        if not reaped:
            print("no stale retry leases found")
        for item in reaped:
            print(f"reaped work_id={item['work_id']} attempt_id={item['reaped_attempt_id']}")
    return 0


def run_ap_batch(
    attempts_path: Path,
    reviews_path: Path,
    out_path: Path,
    *,
    vendor_confidence_threshold: float,
    candidate_human_review_threshold: float,
    candidate_retry_floor: float,
    candidate_max_prior_attempts: int,
) -> int:
    with attempts_path.open("r", encoding="utf-8") as f:
        attempt_rows = json.load(f)
    with reviews_path.open("r", encoding="utf-8") as f:
        review_rows = json.load(f)

    ingested = ingest(attempt_rows, review_rows)
    policies = cast(
        "tuple[BatchPolicy, ...]",
        (
            HumanReviewBaselinePolicy(confidence_threshold=vendor_confidence_threshold),
            RetryOnceBaseline(),
            CandidateBatchPolicy(
                human_review_threshold=candidate_human_review_threshold,
                retry_floor=candidate_retry_floor,
                max_prior_attempts=candidate_max_prior_attempts,
            ),
        ),
    )
    report = build_report(
        ingested.work_records,
        policies,
        total_input_records=len(attempt_rows),
        skipped_rows=ingested.skipped_rows,
        duplicate_attempt_ids=ingested.duplicate_attempt_ids,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2)
        f.write("\n")
    print(
        f"wrote auditable batch report for {len(ingested.work_records)} "
        f"work records to {out_path}"
    )
    return 0


__all__ = [
    "run_ap_batch",
    "run_ap_demo",
    "run_ap_outcome",
    "run_ap_reap",
    "run_ap_report",
]
