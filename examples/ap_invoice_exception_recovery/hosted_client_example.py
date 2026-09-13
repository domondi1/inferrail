"""One coherent walkthrough of the **hosted** AP Exceptions API, run
against a locally started `hosted/ap_exceptions/service.py` (or a real
deployed instance, via `--base-url`).

This is not a collection of separately-exercised endpoints -- it is a
single, real flow: decision -> (locally authorized and executed retry,
or acknowledged human-review handoff) -> recorded resolution ->
retrieved report. The hosted service **never** receives invoice text or
provider credentials -- retry execution happens in this script's own
process, exactly like the local SDK's `RecoveryEngine` does; the hosted
service only ever sees decision metadata, cost figures, and status
enums (see docs/capabilities/ap-invoice-exception-recovery.md's "Data
boundary").

Run the service first (a separate terminal):

    cd hosted/ap_exceptions
    pip install -r requirements.txt && pip install -e ../..
    AP_API_KEYS=dev-key-1 python3 service.py /tmp/inferrail_ap_hosted_example 8422

Then, in this terminal:

    python examples/ap_invoice_exception_recovery/hosted_client_example.py

Or against a real deployed instance:

    python examples/ap_invoice_exception_recovery/hosted_client_example.py \\
        --base-url https://your-deployed-instance --api-key <your-key>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx

from inferrail.ap import AttemptStatus, CostEstimate, ExceptionCase, RetryAttemptResult
from inferrail.ap.adapters import RetryAdapter, get_cost_estimate
from inferrail.ap.policy import authorize_retry_cost
from inferrail.ap.validation import FieldPresenceAndConfidenceValidator

_POLICY_CONFIG: dict[str, Any] = {
    "eligible_failure_types": ["low_confidence", "validation_check_failed"],
    "retry_floor": 0.5,
    "human_review_threshold": 0.75,
    "max_retry_cost_usd": "1.00",
    "decision_deadline_seconds": 86400,
}


@dataclass
class LocalStandInAdapter:
    """Stands in for a real vendor's own re-extraction call -- runs
    entirely in this process, never inside the hosted service. Replace
    `retry`/`estimate_cost` with your own extraction pipeline."""

    name: str = "local_stand_in_adapter"

    def retry(self, case: ExceptionCase) -> RetryAttemptResult:
        return RetryAttemptResult(
            attempt_id=f"{case.work_id}-retry-1",
            status=AttemptStatus.SUCCESS,
            cost_usd=Decimal("0.07"),
            confidence=0.95,
            provider=self.name,
            raw_fields={"invoice_number": "44120", "vendor": "Globex LLC", "total": "980.00"},
        )

    def estimate_cost(self, case: ExceptionCase) -> CostEstimate:
        return CostEstimate(amount_usd=Decimal("0.10"), basis="local_stand_in_adapter flat rate")


@dataclass
class ExampleReviewReceiver:
    """A small working example receiver for your existing review
    system's integration contract -- a file-append stand-in, same spirit
    as `inferrail.ap.handoff.LoggingHandoff`. This release does not
    require (or ship) a general invoice platform or full review
    application; replace this with a real ticketing/queue API call."""

    path: Path
    _counter: int = field(default=0, init=False)

    def send(self, work_id: str, reason: str) -> str:
        self._counter += 1
        ref = f"example_review_receiver:{work_id}:{self._counter}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"handoff_ref": ref, "work_id": work_id, "reason": reason}) + "\n")
        return ref


def _report_row(
    client: httpx.Client, headers: dict[str, str], work_id: str
) -> dict[str, Any] | None:
    resp = client.get("/v1/report", headers=headers)
    resp.raise_for_status()
    rows: list[dict[str, Any]] = resp.json()["rows"]
    return next((r for r in rows if r["work_id"] == work_id), None)


def _send_handoff_and_outcome(
    client: httpx.Client,
    headers: dict[str, str],
    work_id: str,
    review_receiver: ExampleReviewReceiver,
    *,
    reason: str,
) -> None:
    handoff_ref = review_receiver.send(work_id, reason)
    client.post(
        f"/v1/decisions/{work_id}/handoff", json={"handoff_ref": handoff_ref}, headers=headers
    ).raise_for_status()
    print(f"handed off to the example review receiver as {handoff_ref!r}")
    client.post(
        f"/v1/decisions/{work_id}/outcome",
        json={"outcome": "corrected", "correction_delta_usd": "15.00", "review_cost_usd": "3.00"},
        headers=headers,
    ).raise_for_status()
    print("recorded the review receiver's resolution: outcome='corrected'")


def run_hosted_client_walkthrough(
    client: httpx.Client,
    *,
    api_key: str,
    work_id: str,
    review_receiver: ExampleReviewReceiver,
    adapter: RetryAdapter | None = None,
) -> dict[str, Any]:
    """The full flow, against any httpx-compatible client (a real
    `httpx.Client` for a live/local server, or `fastapi.testclient.
    TestClient` in tests) -- returns the final `/v1/report` body.

    **Safe to call more than once for the same `work_id` and store.**
    Calling `POST /v1/decisions` twice is idempotent
    (`idempotent_replay: true` the second time) -- but idempotency at
    that one endpoint is not enough on its own: this function also
    never re-invokes the local retry adapter, and never records a
    second outcome, for a work_id a prior run already decided. A
    repeat run instead inspects the existing state and, if a prior run
    was interrupted before finishing (still `retry_in_progress`),
    recovers safely via `/reap` -- it never re-executes, and therefore
    never causes a second billable provider call.
    """
    adapter = adapter or cast("RetryAdapter", LocalStandInAdapter())
    headers = {"Authorization": f"Bearer {api_key}"}

    decision_body = {
        "work_id": work_id,
        "checkpoint_attempt_id": f"{work_id}-checkpoint",
        "failure_type": "low_confidence",
        "confidence": 0.6,
        "cost_so_far_usd": "0.10",
        "policy_config": _POLICY_CONFIG,
    }
    first = client.post("/v1/decisions", json=decision_body, headers=headers)
    first.raise_for_status()
    decision = first.json()
    print(f"decision: recommended_action={decision['recommended_action']!r} "
          f"status={decision['status']!r} idempotent_replay={decision['idempotent_replay']}")

    # Repeated request -- proves idempotency over the wire, same as the
    # local SDK's `decide()` (see report.build_live_report's callers).
    second = client.post("/v1/decisions", json=decision_body, headers=headers)
    second.raise_for_status()
    assert second.json()["idempotent_replay"] is True
    assert second.json()["decision_id"] == decision["decision_id"]

    already_decided = decision["idempotent_replay"]

    if already_decided and decision["status"] == "retry_in_progress":
        # A prior run of this exact workflow was interrupted before its
        # retry attempt was durably recorded (or before its status was
        # updated) -- never re-invoke the local adapter, whose real-world
        # effect (if it ran at all) is unknown; recover safely instead.
        client.post(f"/v1/decisions/{work_id}/reap", headers=headers).raise_for_status()
        decision = client.get(f"/v1/decisions/{work_id}", headers=headers).json()
        print(f"recovered an interrupted prior run via /reap -- now status={decision['status']!r}")

    case = ExceptionCase(
        work_id=work_id, checkpoint_attempt_id=f"{work_id}-checkpoint",
        failure_type="low_confidence", confidence=0.6, cost_so_far_usd=Decimal("0.10"),
    )

    if decision["recommended_action"] == "retry" and not already_decided:
        # Freshly decided just now -- authorize and execute locally.
        # The hosted service never invokes an adapter itself (see this
        # module's docstring).
        estimate = get_cost_estimate(adapter, case)
        authorized, reason = authorize_retry_cost(
            estimate=estimate, max_retry_cost_usd=Decimal(_POLICY_CONFIG["max_retry_cost_usd"])
        )
        print(f"pre-flight cost authorization: authorized={authorized} ({reason})")

        if authorized:
            result = adapter.retry(case)
            validation = FieldPresenceAndConfidenceValidator(min_confidence=0.9).validate(result)
            client.post(
                f"/v1/decisions/{work_id}/retry-attempts",
                json={
                    "attempt_id": result.attempt_id, "status": result.status.value,
                    "cost_usd": str(result.cost_usd), "confidence": result.confidence,
                    "provider": result.provider, "validation_passed": validation.passed,
                    "validator_version": validation.validator_version,
                    "pre_flight_estimate_usd": str(estimate.amount_usd) if estimate else None,
                },
                headers=headers,
            ).raise_for_status()

            if validation.passed:
                print(f"recovered fields (usable data, not just a status): {result.raw_fields}")
            else:
                _send_handoff_and_outcome(
                    client, headers, work_id, review_receiver, reason="retry failed validation"
                )
        else:
            _send_handoff_and_outcome(client, headers, work_id, review_receiver, reason=reason)
    elif decision["recommended_action"] != "retry" and not already_decided:
        _send_handoff_and_outcome(
            client, headers, work_id, review_receiver, reason=decision["reason"]
        )
    else:
        # already_decided: either a retry already resolved (or was just
        # recovered above), or human review was already decided in a
        # prior run -- never re-invoke the adapter, and never record a
        # second outcome for a resolution a prior run already completed.
        print(
            f"work_id={work_id!r} already processed by a prior run "
            f"(status={decision['status']!r}) -- inspecting existing state, not re-executing"
        )
        existing_row = _report_row(client, headers, work_id)
        needs_completion = (
            decision["status"] == "awaiting_human_review"
            and (existing_row is None or existing_row.get("established_outcome") is None)
        )
        if needs_completion:
            # A prior run reached human review but was interrupted
            # before its handoff/outcome completed -- finish it. Safe
            # to call: the hosted handoff endpoint is idempotent on
            # work_id, and this branch only runs when no outcome is
            # recorded yet, so it can never append a second one.
            _send_handoff_and_outcome(
                client, headers, work_id, review_receiver,
                reason="completing an interrupted prior run's review handoff",
            )

    report_resp = client.get("/v1/report", headers=headers)
    report_resp.raise_for_status()
    return dict(report_resp.json())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8422")
    parser.add_argument("--api-key", default="dev-key-1")
    parser.add_argument("--work-id", default=f"HOSTED-EX-{int(time.time())}")
    args = parser.parse_args()

    review_receiver = ExampleReviewReceiver(
        path=Path("./inferrail-ap-hosted-example-reviews.jsonl")
    )
    try:
        with httpx.Client(base_url=args.base_url, timeout=15.0) as client:
            report = run_hosted_client_walkthrough(
                client, api_key=args.api_key, work_id=args.work_id,
                review_receiver=review_receiver,
            )
    except httpx.ConnectError:
        print(
            f"error: could not connect to {args.base_url} -- start "
            "hosted/ap_exceptions/service.py first (see this file's module docstring).",
            file=sys.stderr,
        )
        return 1

    row = next(r for r in report["rows"] if r["work_id"] == args.work_id)
    print("\nfinal report row for this work_id:")
    print(json.dumps(row, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
