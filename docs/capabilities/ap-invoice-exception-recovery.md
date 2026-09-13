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
  real, billed call; never silently substituted with a fixture.

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

## Persistence, idempotency, and ambiguous execution

SQLite-backed, transactional (`inferrail.ap.store.RecoveryStore`).
Calling `decide()` again for a `work_id` that already has a decision
returns the stored result — the policy is not re-evaluated and the
retry adapter/handoff callback is not invoked again. If the retry
adapter itself is interrupted (raises, times out, crashes) after being
called but before its result is durably recorded, the case is marked
`retry_status: ambiguous` and routed to human review — never silently
retried again (which could re-invoke a real paid provider) and never
assumed successful.

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
DELETE /v1/decisions/{work_id}
GET    /v1/report
```

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
  Authority code paths or storage — fully isolated from all three.

## Getting started

```bash
pip install inferrail          # SDK + CLI (fixture path, zero extra deps)
pip install "inferrail[ap]"    # + OpenAIRetryAdapter's openai dependency
inferrail ap demo              # fixture-based, zero-key walkthrough
```

See [`examples/ap_invoice_exception_recovery/`](../../examples/ap_invoice_exception_recovery/)
for the full walkthrough, including live-provider execution and a custom
integration example.
