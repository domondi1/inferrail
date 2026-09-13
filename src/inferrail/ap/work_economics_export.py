"""The smallest useful connection between AP invoice-exception
recovery's own records and Inferrail's Work Economics reporting.

**What this module supports, precisely.** It exports the (at most two)
cost-bearing facts already recorded for one AP `work_id` -- the retry
attempt's own cost, and a recorded human review's own cost -- as plain
dicts shaped exactly like `hosted/work_economics/capability.py`'s
`EconomicEvent.from_dict()` expects. It does **not** give AP and Work
Economics a shared ledger, a shared database, or a shared process: AP's
`RecoveryStore` (SQLite, one file per tenant) and Work Economics'
`DurablePurchaseStore` remain fully separate storage and service
contracts, exactly as `hosted/ap_exceptions/README.md` and
`docs/adr/0004`/`docs/adr/0010` already require of every hosted
service. This module produces data a caller may *choose* to feed into
`compute_work_economics` (or any other consumer of the same event
shape) -- it does not call that function itself, and it is not itself a
network integration.

**Why this lives here, not in `hosted/`.** `EconomicEvent` lives in
`hosted/work_economics/`, which is deliberately outside the installable
`inferrail` wheel (see `docs/adr/0004`, `docs/adr/0010`) -- the OSS
gateway and the `inferrail.ap` SDK have zero dependency on any hosted
service. Importing `hosted.work_economics` from here would invert that:
an installable package depending on a non-installable one. Instead,
this module produces plain dicts with no import-time dependency on
`hosted/` at all; `tests/unit/ap/test_work_economics_export.py` proves
those dicts are genuinely compatible by round-tripping them through the
real `EconomicEvent.from_dict()`, the same way
`tests/unit/hosted/test_work_economics_capability.py` already reaches
that module (a `sys.path` insert, not a package dependency).

**The `resource_class="human_review"` convention.** `EconomicEvent`
has no field dedicated to distinguishing a machine-attempt cost from a
human-review cost -- `resource_class` is a free string. This module
reserves `"ap_invoice_extraction_retry"` and `"human_review"` as its own
convention on that field, the same way `docs/adr/0008` treats adding a
second `TaskTransaction` resource type as "a visible, additive schema
change... never something that could silently start accepting arbitrary
strings" -- this is that same discipline applied here: a documented
convention, not a silent widening of what `resource_class` means
elsewhere in the codebase.

**Double-counting.** Call this only on the raw store rows for a single
`work_id`; never call it on, or add its output to,
`report.LiveReportRow.observed_cost_usd` (the aggregate) -- the two
events this module can emit are exactly `_observed_cost`'s own two
components (retry cost, review cost), so summing both would double the
same money. `test_work_economics_export.py` asserts the sum of this
module's `known_cost_usd` values equals `observed_cost_usd` whenever
both are known, specifically to guard against that mistake creeping in
later.

**A genuinely unknown review cost is exported, not dropped.** If a
review was recorded but its cost is unknown, this module still emits a
`resource_class="human_review"` event with `price_basis="UNKNOWN"` and
`known_cost_usd=None` -- it is never silently omitted, and never
coerced into the machine-cost resource class where a reader might
mistake it for part of the retry's own cost.
"""

from __future__ import annotations

from typing import Any

from .models import AttemptStatus, ReviewOutcome
from .store import RecoveryStore

_ATTEMPT_STATUS_TO_EVENT_STATUS = {
    AttemptStatus.SUCCESS.value: "success",
    AttemptStatus.FAILED.value: "error",
    AttemptStatus.PARTIAL.value: "partial",
    AttemptStatus.AMBIGUOUS.value: "error",
}

_REVIEW_OUTCOME_TO_EVENT_STATUS = {
    ReviewOutcome.ACCEPTED.value: "success",
    ReviewOutcome.CORRECTED.value: "success",
    ReviewOutcome.REJECTED.value: "success",
    ReviewOutcome.ESCALATED.value: "partial",
}


def export_work_economics_events(store: RecoveryStore, work_id: str) -> list[dict[str, Any]]:
    """Returns 0, 1, or 2 event dicts for `work_id`, shaped for
    `EconomicEvent.from_dict()`:

    - `resource_class="ap_invoice_extraction_retry"`, only if a retry
      attempt was recorded (`RecoveryStore.get_retry_attempt`).
    - `resource_class="human_review"`, only if a review outcome was
      recorded (`RecoveryStore.get_outcome_history`) -- always keyed off
      the *latest* recorded outcome, matching `report.build_live_report`.

    Raises `KeyError` if no decision exists for `work_id` at all (there
    is nothing to export -- never fabricated as an empty-but-valid
    result).
    """
    if store.get_decision(work_id) is None:
        raise KeyError(f"no decision recorded for work_id={work_id!r}")

    events: list[dict[str, Any]] = []

    attempt = store.get_retry_attempt(work_id)
    if attempt is not None:
        cost = attempt["cost_usd"]
        events.append(
            {
                "resource_class": "ap_invoice_extraction_retry",
                "supplier": attempt["provider"],
                "price_basis": "PROVIDER_REPORTED_ESTIMATE" if cost is not None else "UNKNOWN",
                "status": _ATTEMPT_STATUS_TO_EVENT_STATUS.get(attempt["status"], "error"),
                "known_cost_usd": cost,
            }
        )

    outcomes = store.get_outcome_history(work_id)
    if outcomes:
        latest = outcomes[-1]
        cost = latest["review_cost_usd"]
        events.append(
            {
                "resource_class": "human_review",
                "supplier": latest["source"],
                "price_basis": "PROVIDER_REPORTED_ESTIMATE" if cost is not None else "UNKNOWN",
                "status": _REVIEW_OUTCOME_TO_EVENT_STATUS.get(latest["outcome"], "partial"),
                "known_cost_usd": cost,
            }
        )

    return events
