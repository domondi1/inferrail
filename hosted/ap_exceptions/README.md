# Inferrail AP Exceptions (hosted)

A hosted decision, persistence, and reporting service for AP invoice-
exception recovery. See
[`docs/capabilities/ap-invoice-exception-recovery.md`](../../docs/capabilities/ap-invoice-exception-recovery.md)
for the public contract and
[`docs/adr/0004-data-plane-control-plane-boundary.md`](../../docs/adr/0004-data-plane-control-plane-boundary.md)
for why this lives outside `src/inferrail`.

**This service never executes a retry itself.** It runs the same
`inferrail.ap.policy.recommend` decision logic and
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
  retry your own `RetryAdapter` already executed locally.
- `POST /v1/decisions/{work_id}/handoff` — record a human-review
  handoff reference your own system already generated.
- `POST /v1/decisions/{work_id}/outcome` — record a real human-review
  outcome (independently establishes correctness for that work_id).
- `GET /v1/report` — the joined, auditable report for your tenant
  (capped at `AP_MAX_REPORT_ROWS`, default 500 — paginate your own data
  client-side above that by deleting/archiving resolved work_ids).
- `DELETE /v1/decisions/{work_id}` — retention/deletion: irreversibly
  removes every record for one `work_id`.

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
schema (additive `CREATE TABLE IF NOT EXISTS`, safe to run against an
older data directory). To roll back: redeploy the previous image/commit
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
