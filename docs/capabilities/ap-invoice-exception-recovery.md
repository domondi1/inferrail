# AP Invoice-Exception Recovery — v0.2.0

**Status:** first release. Bounded scope — see "What this does not do,"
below, before assuming it covers more than it does.

For one eligible invoice-extraction exception, decide whether it gets
one permitted machine retry or your established human-review path,
execute the retry through a supported integration, and record the
resulting cost and outcome. Ships as a Python SDK (`inferrail.ap`), a
CLI (`inferrail ap ...`), and an optional hosted HTTP API.

## What it does

1. **Decides**, given decision-time-only inputs (never a future
   outcome): retry, human review, or "insufficient evidence" (falls back
   to human review — never a guess).
2. **Executes** the one permitted retry through a `RetryAdapter` you
   supply — your own extraction pipeline, or the bundled
   `OpenAIRetryAdapter` reference implementation.
3. **Validates** the retry's result against declared rules — a passed
   validation is not the same as independently established correctness
   (see below).
4. **Hands off** to your existing human-review path when needed — this
   product does not implement a review queue.
5. **Records** the resulting cost and outcome, durably and idempotently,
   in an inspectable, auditable report.

## First supported failure types

Only these two; any other value gets `insufficient_evidence` rather than
a guess:

- `low_confidence` — a required extracted field's confidence is below
  your configured threshold.
- `validation_check_failed` — a deterministic cross-check you already
  ran (e.g. a required field missing, or line items not summing to the
  stated total) failed, and the case isn't already `low_confidence`.

## Retry method

Exactly one retry per case in this release (`max_retries` is fixed at
`1` in policy schema version `ap.policy/v1`). You supply a `RetryAdapter`
— a plain callable, not a network contract Inferrail owns:

```python
class RetryAdapter(Protocol):
    name: str
    def retry(self, case: ExceptionCase) -> RetryAttemptResult: ...
```

Two reference implementations ship with the SDK:

- `FixtureRetryAdapter` — deterministic, offline, canned results. Used
  by `inferrail ap demo` and this repo's own tests.
- `OpenAIRetryAdapter` — a working, real-provider adapter that re-runs
  structured-field extraction via one OpenAI chat-completions call.
  **Live-provider execution** — requires `OPENAI_API_KEY` and makes a
  real, billed call; never silently substituted with a fixture. Its
  client is constructed with `max_retries=0` and an explicit timeout —
  the `openai` SDK's own default retry behavior would otherwise risk
  more than one real HTTP request per logical `retry()` call.

### Prospective retry-cost authorization

Before the adapter is invoked, the engine asks it for a `CostEstimate` —
a defensible upper bound on what the call is about to cost, with its
basis — via an **optional** `estimate_cost(case) -> CostEstimate | None`
method (detected at runtime, not required by the `RetryAdapter`
protocol itself: an adapter that cannot bound its own cost should not
implement it). `policy.authorize_retry_cost` then checks that estimate
against `max_retry_cost_usd` — separately from, and never confused with,
`recommend`'s own sunk-cost check (`case.cost_so_far_usd` vs. the same
limit, already spent, unaffected by what happens next):

- **Unknown estimate** (adapter has no `estimate_cost`, or it returns
  `None`) → **never authorized**. The adapter is not invoked; the case
  routes to human review with no `retry_attempts` row at all, so the
  report honestly shows no retry was ever attempted.
- **Estimate exceeds `max_retry_cost_usd`** → same: not authorized, not
  invoked.
- **Estimate within budget** → the adapter is called, and its estimate
  is recorded (`pre_flight_estimate_usd`) alongside the attempt.

**Never a hard billing guarantee.** A provider's real, metered charge
can still exceed the estimate that authorized it — this cannot be
undone after the fact. When it happens, the report records the honest
delta as `retry_cost_overrun_usd`, never clamped or hidden. `Fixture
RetryAdapter.estimated_costs_by_work_id` and `OpenAIRetryAdapter`'s
built-in-catalog-based estimate (see `inferrail.pricing.builtin`, the
same sourced, dated rates the OSS gateway uses) are the two shipped
implementations of `estimate_cost`.

## Validation contract

A bundled `FieldPresenceAndConfidenceValidator` (or your own `Validator`)
checks a retry's result and returns a `ValidationResult` — `passed`, plus
per-check detail. **A passed validation means the declared checks
passed, nothing more.** It is not a claim of correctness. Independently
established correctness comes only from a later recorded human-review
outcome (`record_outcome` / `POST /v1/decisions/{work_id}/outcome`) or
your own downstream reconciliation — the report keeps `validation_passed`
and `established_outcome` as distinct fields, never merged or inferred
from one another.

**Confidence is not treated as a calibrated probability, and no policy
here is economic optimization.** `retry_floor`/`human_review_threshold`
are uninterpreted cutoffs on whatever number your extraction pipeline
reports. Every decision states plainly what it *used* (failure type,
confidence, cost-so-far, case age, the configured limits) and
*guarantees* (at most one retry, a fixed fallback to human review on any
unsupported/insufficient case, no fabricated counterfactual) — nothing
beyond that.

## Human-review handoff

```python
class HumanReviewHandoff(Protocol):
    def send(self, case, recommendation, attempt_history) -> str: ...
```

Called exactly once per case that needs review. Inferrail does not
implement a review UI or queue — implement `send` against your own
ticketing/queue system (a bundled `LoggingHandoff` writes a JSON Lines
record for local/demo use).

## Versioned, configurable policy

```python
PolicyConfig(
    eligible_failure_types=frozenset({"low_confidence", "validation_check_failed"}),
    retry_floor=0.5,
    human_review_threshold=0.75,
    max_retry_cost_usd=Decimal("1.00"),
    decision_deadline_seconds=86400,
)
```

Stamped as `policy_version` on every decision for provenance.
`max_retries` (fixed at `1`) and `fallback_action` (fixed at
`human_review`) are part of `config_version` `ap.policy/v1` by design —
not caller-configurable in this release.

## Stable identifiers

`work_id` (yours, stable per exception), `attempt_id` (per machine
attempt), `decision_id` (assigned once per `work_id`). All three appear
on every record.

## Persistence, idempotency, and crash recovery

SQLite-backed, transactional (`inferrail.ap.store.RecoveryStore`).
Calling `decide()` again for a `work_id` that already has a decision
returns the stored result — the policy is not re-evaluated and the
retry adapter/handoff callback is not invoked again. If the retry
adapter itself raises (times out, errors) after being called but before
its result is durably recorded, the case is marked `retry_status:
ambiguous` and routed to human review — never silently retried again
(which could re-invoke a real paid provider) and never assumed
successful.

**Real process death is handled durably, not just an in-process
exception.** A `retry_in_progress` decision carries a lease
(`worker_id`, `lease_expires_at`, set only for that window). If the
process making the retry call is killed outright — not an exception,
an actual crash — the case is left `retry_in_progress` with no one able
to act on it, until the lease expires. At that point:

- `RecoveryEngine.reap_stale_retries()` (or the CLI's `inferrail ap
  reap`, or the hosted API's `POST /v1/reap-stale`) finds every expired
  lease, records a synthetic `ambiguous` retry attempt (`provider:
  lease_reaper`), and moves the decision to `awaiting_human_review` —
  the same terminal, non-guessed state a raised exception produces.
  **Idempotent**: a repeat reap call for an already-reaped or
  already-resolved work_id is a no-op, never a duplicate attempt.
- **Abandoned vs. still-running is checked, not assumed**: reaping
  re-verifies the lease is still expired and that no real attempt has
  been recorded in the meantime before acting — a "dead" worker that
  was actually still running is never overwritten.
- **A late-arriving real result is never silently lost.** If the
  supposedly-dead worker's retry call does eventually complete after
  its lease was reaped, the real result (status, cost) is preserved in
  an audit-only `late_retry_results` record — but the decision's
  authoritative status is never silently flipped back to
  `retry_resolved`, because no receiving system ever acknowledged
  anything about it after the reap.
- **A handoff is never fabricated during a sweep.** `reap_stale_retries`
  deliberately does not send a handoff itself — the store doesn't
  persist enough of the original `ExceptionCase` to reconstruct one
  without guessing. `RecoveryEngine.ensure_handoff(case)` (idempotent,
  safe to call repeatedly) completes the handoff once a caller
  re-supplies the case — the same method also recovers from a handoff
  callback that itself raised (`HandoffSendFailed`), since a stored
  status must never claim a handoff succeeded when no receiving system
  acknowledged it.

## Explicit handling

- **Already-resolved work** — excluded from the batch/report path, never
  given a spurious "retry" recommendation.
- **Unsupported histories** — event sequences outside this release's
  single-shot model are quarantined with a reason, never guessed at.
- **Unknown costs** — never coerced to zero; an incomplete subtotal is
  flagged as such.
- **Failed retries** — recorded as a real, observed cost and outcome,
  routed to human review.
- **Delayed corrections** — a later review revision is preserved
  (outcome history), never silently overwritten.

## Data boundary

Invoice contents and provider credentials stay in your own process. The
SDK runs client-side; adapters (including `OpenAIRetryAdapter`) call the
external provider directly from your process. If you use the hosted API,
it receives only identifiers, policy-config numbers,
confidence/validation results, cost figures, and status enums — **never**
invoice field values or provider credentials.

## Hosted HTTP API (optional)

If you'd rather not run your own store, an authenticated hosted API
performs the decide/persist/report steps remotely — retry execution
still always happens in your own process. See
[`hosted/ap_exceptions/README.md`](../../hosted/ap_exceptions/README.md)
for the full contract, env vars, and deployment instructions.

```
GET    /health
POST   /v1/decisions
GET    /v1/decisions/{work_id}
POST   /v1/decisions/{work_id}/retry-attempts
POST   /v1/decisions/{work_id}/handoff
POST   /v1/decisions/{work_id}/outcome
POST   /v1/decisions/{work_id}/reap
POST   /v1/reap-stale
DELETE /v1/decisions/{work_id}
GET    /v1/report
```

`/reap` and `/reap-stale` are the hosted-service analog of `inferrail ap
reap` (see "Persistence, idempotency, and crash recovery," above) —
tenant-scoped, safe to call on a schedule, and idempotent. Because the
hosted service never executes your retry adapter itself (see "Data
boundary," below), a caller using the hosted API is responsible for
running `estimate_cost`/`authorize_retry_cost` locally, before calling
`.../retry-attempts` — see
[`examples/ap_invoice_exception_recovery/hosted_client_example.py`](../../examples/ap_invoice_exception_recovery/hosted_client_example.py)
for the full pattern.

A running instance auto-serves its own OpenAPI schema at
`GET /openapi.json` — the authoritative, always-current machine-readable
reference for this service (the repo's committed `openapi.json` at the
root covers only the OSS gateway's contract, generated by
`scripts/generate_openapi.py`; forcing a second service's routes into
that file would make it drift from whichever is actually deployed).

## Work Economics connector

The smallest useful connection between this module's own records and
Inferrail's separate Work Economics reporting:
[`inferrail.ap.work_economics_export.export_work_economics_events`](../../src/inferrail/ap/work_economics_export.py)
reads one `work_id`'s recorded retry attempt and/or review outcome from
`RecoveryStore` and returns 0–2 plain dicts shaped exactly like
`hosted/work_economics/capability.py`'s `EconomicEvent.from_dict()`
expects — tested by round-tripping through the real contract, not a
guessed shape.

**What this is, precisely — and what it is not:**

- It exports the two cost components AP already tracks: the retry
  attempt's own cost (`resource_class="ap_invoice_extraction_retry"`)
  and a recorded human review's own cost
  (`resource_class="human_review"`) — a documented convention on
  `EconomicEvent`'s free-text `resource_class` field, not a schema
  change to it.
- It is **not** a shared ledger. AP's `RecoveryStore` (SQLite, per
  tenant) and Work Economics' `DurablePurchaseStore` remain fully
  separate storage and service contracts — this function reads AP's own
  store and returns plain dicts; it does not call, import, or depend on
  any hosted service.
- **Never double-counted**: call it only on one `work_id`'s raw store
  rows, never on `report.LiveReportRow.observed_cost_usd` (the
  aggregate) — the sum of what it exports equals that aggregate exactly
  when both are known, by construction (they're the same two numbers).
- **A human review's cost is a real cost Work Economics' own event
  contract has no dedicated field for** — a genuinely unknown review
  cost is exported with `price_basis="UNKNOWN"`, never silently dropped
  and never folded into the machine-cost resource class where it could
  be misread as part of the retry's own cost.

This is a supported connection for a caller who already has a Work
Economics consumer and wants AP's costs represented the same way — it
is not a claim that AP and Work Economics already share one ledger.

## Pricing and performance assumptions — explicitly labeled, not validated

- **Assumption, not a claim:** if ever priced, a per-decision or
  per-integration fee is the plausible shape. No billing system ships
  with this release.
- **Assumption, not a claim:** one permitted retry costs materially less
  than a human review in the median case. This is exactly what the
  auditable report is built to check against your own historical data —
  not something asserted true here.
- No claim of proven savings or customer adoption is made anywhere in
  this documentation.

## What this does not do

- Does not extract invoices, approve payment, or replace your review
  queue.
- Does not model more than one retry per exception, or failure types
  beyond the two listed above.
- Does not compute or imply a calibrated probability, an economic
  optimum, or an ROI figure.
- Does not touch Inferrail's OSS gateway, Work Economics, or Economic
  Authority code paths or storage — fully isolated from all three. The
  Work Economics connector above is a same-process data export, not a
  shared database, shared ledger, or dependency between the services.

## Platform notes

Verified natively on Windows, macOS, and Linux —
[`.github/workflows/platform-verify.yml`](../../.github/workflows/platform-verify.yml)
builds the wheel and installs it into a clean venv on each OS
(`windows-latest`, `macos-latest`, `ubuntu-latest`, not an emulated
container), then runs the documented demo/report commands, the local
integration example, the hosted-API client against a locally started
instance, and the persistence/crash-recovery test suite (including a
real concurrent-writer race and real process-kill tests) against that
installed package specifically — not an editable checkout.

Tested command sequences — install, run the demo, find its output, stop
it:

**macOS / Linux (bash):**
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install "inferrail[ap]"
inferrail ap demo
cat ./inferrail-ap-demo-handoffs.jsonl   # the demo's own output files
inferrail ap report --db inferrail-ap-demo.sqlite3
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install "inferrail[ap]"
inferrail ap demo
Get-Content .\inferrail-ap-demo-handoffs.jsonl
inferrail ap report --db inferrail-ap-demo.sqlite3
```

The CLI commands above are one-shot — there is nothing to "stop." The
only long-running process this release ships is the hosted service
(`hosted/ap_exceptions/service.py`, run locally for
`hosted_client_example.py` or self-hosted for real); stop it the normal
way for a foreground process (`Ctrl-C` on macOS/Linux/Windows), or
`Stop-Process` on Windows / your process manager's stop command if you
ran it in the background or under a supervisor.

## Getting started

```bash
pip install inferrail          # SDK + CLI (fixture path, zero extra deps)
pip install "inferrail[ap]"    # + OpenAIRetryAdapter's openai dependency
inferrail ap demo              # fixture-based, zero-key walkthrough
```

See [`examples/ap_invoice_exception_recovery/`](../../examples/ap_invoice_exception_recovery/)
for the full walkthrough, including live-provider execution and a custom
integration example.
