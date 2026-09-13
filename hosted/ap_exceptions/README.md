# Inferrail AP Exceptions (hosted)

A hosted decision, persistence, and reporting service for AP invoice-
exception recovery. See
[`docs/capabilities/ap-invoice-exception-recovery.md`](../../docs/capabilities/ap-invoice-exception-recovery.md)
for the public contract and
[`docs/adr/0004-data-plane-control-plane-boundary.md`](../../docs/adr/0004-data-plane-control-plane-boundary.md)
for why this lives outside `src/inferrail`.

**Hosted demonstration, not a durable service and not a usable public
demo of the workflow itself:** `https://inferrail-ap-exceptions.onrender.com`
runs on Render's free tier with no persistent disk — every record on
it is synthetic and disposable, gone on the next restart or idle
spin-down, and it is never used for real customer data. Without a key,
`GET /health` is the only reachable route — that confirms the process
is up, nothing more; every other route needs an `Authorization: Bearer
<api-key>` header this demo does not distribute publicly. The free
tier also spins down when idle, so the first request after a quiet
period may take a minute or longer to wake it (no guaranteed upper
bound). **To actually try the decide → retry → validate →
recover-or-review → report workflow, run `inferrail ap demo`** (see
the package README) — it exercises the real decision engine locally,
no key or network call required. Deploy your own hosted instance
(below) for real, durable, authenticated use.

**This service never executes a retry itself.** It runs the same
`inferrail.ap.policy.recommend` policy evaluation and
`inferrail.ap.store.RecoveryStore` persistence the local SDK uses, over
HTTP, per authenticated tenant. Retry execution always happens in your
own process (a `RetryAdapter`) — invoice content and provider credentials
never reach this service; see the capability doc's "Data boundary."

## Isolation from other hosted services

Separate module, separate process, separate storage directory from
`hosted/work_economics` and `hosted/a2a_economic_authority` — this
service does not read, write, or share a database file, port, or process
with either. Each tenant (API key) additionally gets its own SQLite file
under this service's own data directory (see `tenant_store.py`).

## Running it locally

```bash
cd hosted/ap_exceptions
pip install -r requirements.txt
pip install -e ../..              # installs `inferrail` (for inferrail.ap) from this checkout
export AP_API_KEYS=dev-key-1,dev-key-2   # comma-separated; each key is its own isolated tenant
python3 service.py /tmp/inferrail_ap_test 8422
```

Then, from another terminal:

```bash
curl http://127.0.0.1:8422/health

curl -X POST http://127.0.0.1:8422/v1/decisions \
  -H "Authorization: Bearer dev-key-1" -H "Content-Type: application/json" -d '{
    "work_id": "INV-1001",
    "checkpoint_attempt_id": "INV-1001-checkpoint",
    "failure_type": "low_confidence",
    "confidence": 0.6,
    "cost_so_far_usd": "0.10",
    "policy_config": {
      "eligible_failure_types": ["low_confidence", "validation_check_failed"],
      "retry_floor": 0.5,
      "human_review_threshold": 0.75,
      "max_retry_cost_usd": "1.00",
      "decision_deadline_seconds": 86400
    }
  }'
```

## API surface

All routes except `/health` require `Authorization: Bearer <api-key>`.

- `GET /health` — liveness check, no auth required.
- `POST /v1/decisions` — decide retry vs. human review for one case.
  Idempotent on `work_id`: a repeat with the same `work_id` returns the
  original decision (`idempotent_replay: true`), never re-evaluated.
- `GET /v1/decisions/{work_id}` — fetch one decision.
- `POST /v1/decisions/{work_id}/retry-attempts` — record the result of a
  retry your own `RetryAdapter` already executed locally. Transitions
  the decision's status off `retry_in_progress` only when the attempt's
  own `status` is exactly `success` *and* `validation_passed` is `true`
  (to `retry_resolved`; otherwise `awaiting_human_review`, mirroring the
  local SDK engine's own transition exactly) so `/v1/report`'s
  `observed_cost_complete` can become `true`. **This service never
  invokes your adapter itself** — before calling this endpoint, run
  `inferrail.ap.adapters.get_cost_estimate` and
  `inferrail.ap.policy.authorize_retry_cost` locally to decide whether
  to invoke your adapter at all, and pass the resulting
  `pre_flight_estimate_usd` here for an honest overrun to be recorded if
  the real cost exceeds it. See
  [`examples/ap_invoice_exception_recovery/hosted_client_example.py`](../../examples/ap_invoice_exception_recovery/hosted_client_example.py)
  for the full pattern.
  - **`422`** if `validation_passed=true` is paired with any `status`
    other than `success` — a failed or interrupted attempt cannot have
    "passed validation," and this is rejected explicitly rather than
    silently accepted as `retry_resolved`.
  - **`409`** if a *different* attempt already exists for this work_id
    (typically because its lease was already reaped) — never an
    unhandled `500`. The real result is still durably recorded (for
    audit, via `late_retry_results`) and becomes visible in
    `GET /v1/report`'s `late_result_status`/`late_result_cost_usd`, but
    the decision's own accepted status is never changed by it.
- `POST /v1/decisions/{work_id}/handoff` — record a human-review
  handoff reference your own system already generated.
- `POST /v1/decisions/{work_id}/outcome` — record a real human-review
  outcome (independently establishes correctness for that work_id).
- `POST /v1/decisions/{work_id}/reap` — operator recovery for one
  work_id whose retry lease has expired (your own caller's process died
  after receiving a `retry_in_progress` decision but before calling back
  `/retry-attempts`). Response `kind` says which of two things happened:
  `"reaped"` (no real attempt was ever recorded -- a synthetic
  `ambiguous` one is inserted) or `"reconciled"` (a real attempt WAS
  already recorded before the crash -- its own validation result decides
  the terminal status, never re-guessed and never duplicated). Idempotent:
  a repeat call once the lease is no longer stale returns
  `{"reaped": false, "kind": null}`.
- `POST /v1/reap-stale` — sweeps every stale retry lease for your
  tenant. Safe to call on a schedule (a cron job, a health-check
  companion task).
- `GET /v1/report` — the joined, auditable report for your tenant
  (capped at `AP_MAX_REPORT_ROWS`, default 500 — paginate your own data
  client-side above that by deleting/archiving resolved work_ids).
- `DELETE /v1/decisions/{work_id}` — retention/deletion: irreversibly
  removes every record for one `work_id`.

A running instance also auto-serves `GET /openapi.json` (FastAPI's
built-in schema) — the authoritative, always-current machine-readable
reference for this exact service.

### Recovering interrupted work (crash recovery)

If your own process dies after `POST /v1/decisions` returns a
`retry_in_progress` decision but before you call back
`/retry-attempts`, that decision's lease (`lease_seconds` in the
request body, default 300s) eventually expires. Recover it with:

```bash
curl -X POST http://127.0.0.1:8422/v1/reap-stale -H "Authorization: Bearer dev-key-1"
# {"reaped_count": 1, "reaped_work_ids": ["INV-1001"]}
```

This moves the decision to `awaiting_human_review` with a synthetic
`ambiguous` retry attempt recorded — it never re-invokes your adapter
(this service never had it to begin with), and never assumes the
interrupted attempt succeeded. Run this on a schedule (a cron job or a
periodic health-check companion task) so interrupted work doesn't sit
stuck indefinitely.

## Environment variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `AP_API_KEYS` | **yes** | none (service refuses all requests) | Comma-separated list of valid API keys. Each key is its own isolated tenant. |
| `AP_DATA_DIR` | no | `/tmp/inferrail_ap_exceptions` | Directory holding one SQLite file per tenant. **Must be a persistent volume in production** — see "Persistence" below. |
| `PORT` | no (prod) | `8422` | Injected by most hosting platforms; used when no CLI port arg is given. |
| `AP_RATE_LIMIT_MAX_REQUESTS` | no | `120` | Max requests per tenant per window. |
| `AP_RATE_LIMIT_WINDOW_SECONDS` | no | `60` | Rate-limit window, in seconds. |
| `AP_REQUEST_TIMEOUT_SECONDS` | no | `10` | Per-request processing timeout; exceeding it returns `504`. |
| `AP_MAX_REPORT_ROWS` | no | `500` | Max rows `GET /v1/report` returns in one call. |

## Persistence

SQLite, one file per tenant, under `AP_DATA_DIR`. **On a platform with an
ephemeral filesystem (e.g. Render's default web service disk), attach a
persistent disk and point `AP_DATA_DIR` at it** — otherwise every
redeploy or restart silently loses all decision/attempt/outcome history.
This mirrors `hosted/work_economics`'s own persistence model exactly;
see that service's README for the same caveat.

**On Supabase:** this release deliberately does not use Supabase for AP
storage. The only confirmed Supabase usage in this repository is the
marketing `docs/join/index.html` early-access form (see
`ops/deployments.md` in the private repo) — no hosted service uses it.
Introducing a new datastore dependency for a bounded first release would
add risk without a validated reason; the SQLite-per-tenant model already
matches this repo's proven `hosted/work_economics` pattern. Revisit if a
real deployment needs cross-instance/shared storage Supabase would
provide and SQLite-on-a-disk does not.

## Deploying it (Render, or any host that runs a long-lived Python process)

1. Build command: `pip install -r hosted/ap_exceptions/requirements.txt && pip install -e .`
   (run from the repo root, so `inferrail.ap` is importable).
2. Start command: `python3 hosted/ap_exceptions/service.py` (no CLI args
   — binds `0.0.0.0`, reads `$PORT`).
3. Attach a persistent disk and set `AP_DATA_DIR` to a path on it.
4. Set `AP_API_KEYS` to the real key(s) you're issuing. Never commit
   these; set them as the platform's secret environment variables.
5. Health check path: `/health`.

## Rollback

This service keeps no server-side migration state beyond the SQLite
schema (additive `CREATE TABLE IF NOT EXISTS` plus a small number of
`ALTER TABLE ADD COLUMN` migrations for columns introduced after a
tenant's database file was first created — both safe to run against an
older data directory, and skipped automatically if already applied). To
roll back: redeploy the previous image/commit
against the same `AP_DATA_DIR` persistent disk — older code never
deletes columns or tables newer code added, so a rollback only loses
access to fields the newer code introduced, never data. Verify with
`GET /health` and one `GET /v1/report` call against a known tenant after
rolling back.

## Access, retention, and deletion

- Access is controlled entirely by possession of a valid `AP_API_KEYS`
  entry — there is no self-serve key issuance or per-key scoping beyond
  tenant isolation in this release.
- This service sets no automatic retention/expiry policy on its own —
  data for a `work_id` persists until the tenant calls
  `DELETE /v1/decisions/{work_id}`. Operators running this for a real
  customer should agree a retention period with them and enforce it
  operationally (a scheduled job calling `DELETE` on resolved,
  past-retention work_ids), matching the same procedural (not automatic)
  discipline the private repo's data-handling notes describe for the
  historical batch/analysis path.
