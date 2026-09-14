# Inferrail AP Exceptions (hosted)

A hosted decision, persistence, and reporting service for AP invoice-
exception recovery. See
[`docs/capabilities/ap-invoice-exception-recovery.md`](../../docs/capabilities/ap-invoice-exception-recovery.md)
for the public contract and
[`docs/adr/0004-data-plane-control-plane-boundary.md`](../../docs/adr/0004-data-plane-control-plane-boundary.md)
for why this lives outside `src/inferrail`.

**Hosted demonstration, not a durable service:**
`https://inferrail-ap-exceptions.onrender.com` runs on Render's free
tier with no persistent disk — every record on it is synthetic and
disposable, gone on the next restart or idle spin-down, and it is never
used for real customer data. The free tier also spins down when idle,
so the first request after a quiet period may take a minute or longer
to wake it (no guaranteed upper bound). Deploy your own hosted instance
(below) for real, durable, authenticated use.

**No account needed to run the real hosted workflow yourself.**
`POST /v1/sandbox` (no auth) issues you your own short-lived, isolated
API key — no signup, no waiting on a human. See "Try it yourself: the
self-serve sandbox" below for the exact four commands. `inferrail ap
demo` (see the package README) is still the fastest way to exercise the
decision engine with zero key and zero network call, if that's all you
need.

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

## Try it yourself: the self-serve sandbox

No account, no self-hosting, no human on the other end — run this against
the live demo instance right now. Every response is a real decision
running through the real policy engine, tagged `"sandbox": true` and
scoped to a tenant only you can see.

```bash
# 1. Get a sandbox key (no account needed)
curl -s -X POST https://inferrail-ap-exceptions.onrender.com/v1/sandbox
```

Copy the `api_key` value from the response, then paste it here (the
sandbox tenant is scoped entirely to this one key — nothing else to
configure):

```bash
export API_KEY=sbx_paste-your-key-here
```

```bash
# 2. Create a decision
curl -s -X POST https://inferrail-ap-exceptions.onrender.com/v1/decisions \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" -d '{
    "work_id": "SANDBOX-DEMO-1",
    "checkpoint_attempt_id": "SANDBOX-DEMO-1-checkpoint",
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

```bash
# 3. Record a retry attempt (as if your own RetryAdapter had just run it)
curl -s -X POST https://inferrail-ap-exceptions.onrender.com/v1/decisions/SANDBOX-DEMO-1/retry-attempts \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" -d '{
    "attempt_id": "SANDBOX-DEMO-1-attempt-1",
    "status": "success",
    "cost_usd": "0.06",
    "validation_passed": true,
    "validator_version": "ap.validator/v1"
  }'
```

```bash
# 4. Read your own report
curl -s https://inferrail-ap-exceptions.onrender.com/v1/report \
  -H "Authorization: Bearer $API_KEY"
```

**What to expect:** step 4's `rows` includes `work_id: "SANDBOX-DEMO-1"`
with `status: "retry_resolved"` and `observed_cost_complete: true`.
Every response above carries `"sandbox": true` and a `sandbox_notice`
field.

**Guardrails, so this stays usable for everyone:** a sandbox key expires
(`expires_at` in step 1's response — issuing a new one after expiry
returns a clear `401` naming the expiry time, not a generic "invalid
key" error), is capped at a small number of decisions per key, is
rate-limited per key exactly like an operator key, and issuance itself
is throttled per IP address. If step 1 returns `503`, the sandbox is
temporarily at capacity or has been disabled by the operator — try again
shortly, or self-host your own instance (below) for unthrottled,
durable use. See "Environment variables" for the exact defaults.

**Cold start:** if the demo instance has been idle, step 1 (or any
step) may take up to a minute or more to respond the first time —
Render's free tier spins the process down when idle. This is not a bug;
the second request is fast.

## API surface

All routes except `/health` and `POST /v1/sandbox` require
`Authorization: Bearer <api-key>` — a self-serve sandbox key from
`POST /v1/sandbox` works identically to an operator-provisioned one on
every route below, scoped to its own isolated, capped, auto-expiring
tenant.

- `POST /v1/sandbox` — no auth. Issues `{api_key, tenant_id, expires_at,
  ttl_seconds, max_rows_per_tenant, rate_limit}` for a brand-new,
  isolated sandbox tenant. See "Try it yourself" above.

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
| `AP_MAX_REQUEST_BODY_BYTES` | no | `65536` | Abuse guard: any request (sandbox or operator) with a declared `Content-Length` above this is rejected `413` before its body is parsed. |
| `AP_SANDBOX_ENABLED` | no | `true` | Kill switch. Set to `false` to make `POST /v1/sandbox` refuse all new issuance (`503`) — existing sandbox tenants keep working until they expire. |
| `AP_SANDBOX_TTL_SECONDS` | no | `1800` | How long a sandbox key stays valid after issuance. |
| `AP_SANDBOX_MAX_LIVE_TENANTS` | no | `200` | Global ceiling on not-yet-expired sandbox tenants at once; issuance returns `503` above it. |
| `AP_SANDBOX_MAX_ROWS_PER_TENANT` | no | `20` | Max distinct `work_id` decisions one sandbox tenant may create; `POST /v1/decisions` for a *new* `work_id` returns `429` above it (an idempotent replay of an existing `work_id` is never blocked). |
| `AP_SANDBOX_ISSUE_MAX_PER_IP` | no | `5` | Max `POST /v1/sandbox` calls one IP address may make per `AP_SANDBOX_ISSUE_WINDOW_SECONDS`; `429` above it. |
| `AP_SANDBOX_ISSUE_WINDOW_SECONDS` | no | `3600` | Window for the per-IP issuance throttle above. |
| `AP_SANDBOX_PURGE_GRACE_SECONDS` | no | `300` | How long an expired sandbox tenant's record is kept (so a reused expired key gets a clear "expired" `401`, not "invalid") before it and its SQLite file are purged. |
| `AP_SANDBOX_PURGE_INTERVAL_SECONDS` | no | `60` | How often a background task sweeps and purges expired sandbox tenants, independent of traffic. |

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

- Two ways to get a key: an operator adds one to `AP_API_KEYS` (durable,
  no expiry, for real use), or anyone self-issues a sandbox key via
  `POST /v1/sandbox` (no account, isolated, capped, and auto-expiring —
  see "Try it yourself" above and `sandbox.py`). Both authenticate the
  same way and get the same per-tenant isolation; a sandbox tenant is
  simply bounded in size and lifetime and is labeled `sandbox: true` in
  every response.
- Operator-provisioned tenant data has no automatic retention/expiry
  policy — it persists until the tenant calls
  `DELETE /v1/decisions/{work_id}`. Operators running this for a real
  customer should agree a retention period with them and enforce it
  operationally (a scheduled job calling `DELETE` on resolved,
  past-retention work_ids), matching the same procedural (not automatic)
  discipline the private repo's data-handling notes describe for the
  historical batch/analysis path.
- Sandbox tenant data *is* automatically purged: `AP_SANDBOX_TTL_SECONDS`
  after issuance, the key stops authenticating, and roughly
  `AP_SANDBOX_PURGE_GRACE_SECONDS` after that its entire SQLite file is
  deleted (a background sweep runs every `AP_SANDBOX_PURGE_INTERVAL_SECONDS`
  regardless of traffic — see `sandbox.SandboxRegistry.purge_expired`).
