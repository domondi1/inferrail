# Inferrail Cost Gateway (hosted, self-serve trial)

A hosted, one-click, no-account trial of Inferrail's gateway: any visitor
gets a short-lived, isolated tenant, can send zero-key demo traffic
immediately, and may optionally submit their own OpenAI and/or Anthropic
API key to proxy real traffic through their own key and see a real,
payload-free receipt.

**Deployment status: NOT YET DEPLOYED.** No Inferrail-operated public
instance of this service exists yet -- this is Phase 1 (backend
foundation) of a phased build; there is no public URL to point at today.
Run it yourself locally per "Running it locally" below. This will be
corrected here and on the website the moment a real, founder-authorized
deployment exists -- see `hosted/ap_exceptions/README.md` for the exact
kind of overclaim this project has previously found and fixed on its own
public pages, and treats as a real defect, not a rounding error.

Lives outside `src/inferrail`, exactly like `hosted/work_economics`,
`hosted/a2a_economic_authority`, and `hosted/ap_exceptions` (see
`docs/adr/0004`, `docs/adr/0010`, `docs/adr/0012`). The self-hosted
`inferrail serve` CLI path has zero dependency on this service and is
completely unaffected by its existence.

## Isolation from other hosted services

This service never reads, writes, or shares a process, directory, or
connection with `hosted/work_economics`, `hosted/a2a_economic_authority`,
or `hosted/ap_exceptions`. It has its own storage
(`COST_GATEWAY_DATA_DIR`, two SQLite files per tenant -- see
`tenant_store.py`) and its own tenant model (self-serve trial tenants
only; there is no operator-provisioned API key concept in this service
at all, unlike `hosted/ap_exceptions`).

## Threat model and key handling -- restated here, as required every time this service's key handling is touched

A submitted provider key is the most sensitive thing this service ever
receives. Four adversaries, and how each is mitigated, in full in
`keys.py`'s module docstring; summarized here:

1. **Passive log/telemetry leakage** -- mitigated: a key is never logged
   anywhere, and the one place a malformed key could otherwise leak into
   an HTTP response body (a pydantic validation error echoing the
   submitted value) is closed by validating key shape in Python inside
   the route handler, never as a pydantic field constraint (see
   `service.py`'s `_validate_key_shape`).
2. **Data-at-rest compromise** -- mitigated by construction: a key is
   held only in this process's memory (`keys.KeyVault`, a plain `dict`),
   never written to any SQLite file, never included in a receipt or an
   export. **A process restart or crash loses every key in memory** --
   an accepted, documented trade-off, not an oversight: the visitor is
   told plainly (via the trial-status `notice` field and the eventual
   UI copy) that this can happen.
3. **Cross-tenant leakage** -- mitigated: keys are looked up strictly by
   `tenant_id`, derived from the bearer token FastAPI's own
   `_authenticated_tenant` dependency already validated; there is no
   bulk/iteration accessor a route could misuse across tenants. See
   `tests/unit/hosted/test_cost_gateway_service.py`'s adversarial
   cross-tenant test.
4. **Over-broad use** -- mitigated: a key is read from `KeyVault`
   exactly once per request, immediately before constructing that
   request's own ephemeral `Provider`, used for nothing except proxying
   that tenant's own call, and the `Provider` (and its underlying
   `httpx.AsyncClient`) is closed immediately after -- see
   `service.py`'s `_stream_and_close` and the `finally: await
   provider.aclose()` blocks in `chat_completions`/`messages`.

**A key never survives longer than 4 hours after being submitted, and
never longer than the trial's original 24-hour ceiling** -- see
`trial.py`'s `Tenant.tighten_for_real_key`. Every response that reports
trial status includes a live `seconds_remaining` countdown specifically
so this is visible to the visitor continuously, not a one-time notice.

**Self-hosting remains the zero-custody option.** A visitor who does not
want to hand a key to any hosted process at all should use
`pip install inferrail && inferrail serve --quickstart` instead -- this
service exists to remove friction for people who are fine with a
short-lived, in-memory, single-purpose custody arrangement, not to
replace that choice.

## Running it locally

```bash
pip install -e ".[dev]"          # from the repo root
pip install -r hosted/cost_gateway/requirements.txt
python3 hosted/cost_gateway/service.py /tmp/cost_gateway_data 8423
```

`GET http://127.0.0.1:8423/health` should return `{"status": "ok"}`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `COST_GATEWAY_DATA_DIR` | `/tmp/inferrail_cost_gateway` | Where per-tenant SQLite files live (production shape only -- the local/loopback test shape takes `data_dir` as `argv[1]` instead). |
| `PORT` | `8423` | Production shape only; the platform injects this (e.g. Render). |
| `COST_GATEWAY_TRIAL_ENABLED` | `true` | Kill switch for new trial issuance; existing trials keep working until they expire. |
| `COST_GATEWAY_DEMO_TTL_SECONDS` | `86400` (24h) | Demo-mode trial expiry, founder-confirmed default. |
| `COST_GATEWAY_REAL_KEY_TTL_SECONDS` | `14400` (4h) | Real-key trial expiry from the moment a key is submitted, founder-confirmed default -- always tightens, never extends, the demo-mode expiry. |
| `COST_GATEWAY_MAX_LIVE_TENANTS` | `500` | Global ceiling on not-yet-expired trial tenants. |
| `COST_GATEWAY_ISSUE_MAX_PER_IP` / `COST_GATEWAY_ISSUE_WINDOW_SECONDS` | `5` / `3600` | Per-IP throttle on `POST /v1/trial` issuance itself. |
| `COST_GATEWAY_PURGE_GRACE_SECONDS` / `COST_GATEWAY_PURGE_INTERVAL_SECONDS` | `300` / `60` | Expired-tenant purge grace period and background sweep interval. |
| `COST_GATEWAY_RATE_LIMIT_MAX_REQUESTS` / `COST_GATEWAY_RATE_LIMIT_WINDOW_SECONDS` | `60` / `60` | Per-tenant request-rate limit across every authenticated route. |
| `COST_GATEWAY_DAILY_BUDGET_USD` | `1.00` | Each tenant's own daily spend cap, block mode -- enforced pre-flight before any real provider is ever called, via the same `BudgetEnforcer` `inferrail serve`'s budgets use. |
| `COST_GATEWAY_REQUEST_TIMEOUT_SECONDS` | `120` | Whole-request timeout, including a streaming response's full duration -- see "Known limitations" below. |
| `COST_GATEWAY_MAX_REQUEST_BODY_BYTES` | `262144` (256 KiB) | Request body size guard, applied to every route including the unauthenticated `POST /v1/trial`. |

## API contract

Every authenticated route takes `Authorization: Bearer <trial-api-key>`,
the value returned by `POST /v1/trial`.

```bash
# 1. Start a trial -- no auth, no account.
curl -s -X POST http://127.0.0.1:8423/v1/trial | tee trial.json
API_KEY=$(python3 -c "import json;print(json.load(open('trial.json'))['api_key'])")
TENANT_ID=$(python3 -c "import json;print(json.load(open('trial.json'))['tenant_id'])")

# 2. Send a zero-key demo request -- always available, no key needed.
curl -s -X POST http://127.0.0.1:8423/v1/demo/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "default", "messages": [{"role": "user", "content": "hello"}]}'

# 3. Check trial status -- expiry countdown, which keys are configured.
curl -s http://127.0.0.1:8423/v1/trial/$TENANT_ID -H "Authorization: Bearer $API_KEY"

# 4. Add a real OpenAI key (optional). Never echoed back in any response.
curl -s -X POST http://127.0.0.1:8423/v1/trial/$TENANT_ID/keys \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"openai_key": "sk-..."}'

# 5. Send real traffic through that key -- full OpenAI-compatible passthrough.
curl -s -X POST http://127.0.0.1:8423/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -H "X-Inferrail-Attribute-Customer: acme" \
  -d '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Say hi in five words."}]}'

# 6. Read back your own receipts -- payload-free, no prompt/response content.
curl -s "http://127.0.0.1:8423/v1/receipts?limit=10" -H "Authorization: Bearer $API_KEY"

# 7. Forget your key without ending the trial.
curl -s -X DELETE http://127.0.0.1:8423/v1/trial/$TENANT_ID/keys -H "Authorization: Bearer $API_KEY"

# 8. End the trial entirely, right now.
curl -s -X DELETE http://127.0.0.1:8423/v1/trial/$TENANT_ID -H "Authorization: Bearer $API_KEY"
```

`POST /v1/messages` is the Anthropic-compatible equivalent of step 5,
gated on an `anthropic_key` submitted in step 4 instead. Streaming
(`"stream": true`) is supported on both `/v1/chat/completions` and
`/v1/messages`, real SSE passthrough exactly like `inferrail serve`.

Every response is JSON; every error follows the same `ErrorResponse`
shape (`error.message`/`error.type`/`error.code`/`error.remediation`/
`error.docs_url`) the self-hosted gateway already uses (`gateway/app.py`)
-- reused directly, not reinvented, for this service.

## How a frontend would call this (Phase 2 preview)

Phase 2 builds the actual one-click website flow. The contract above is
already everything it needs: `POST /v1/trial` on the "Try Free" click,
immediately show `base_url` + a "send a test request" button wired to
`POST /v1/demo/chat/completions`, a password-style input wired to
`POST /v1/trial/{tenant_id}/keys` for the optional real-key step, and a
live countdown driven by polling `GET /v1/trial/{tenant_id}` (or, in a
later phase, a push-based equivalent) for `seconds_remaining`.

## Known Phase 1 limitations, documented rather than hidden

- **Single-instance only.** In-memory trial/rate-limit state
  (`trial.TrialRegistry`, `auth.RateLimiter`, `keys.KeyVault`) is
  process-local -- multiple instances behind a load balancer would each
  have their own uncoordinated view. Do not run more than one instance
  of this service against the same `COST_GATEWAY_DATA_DIR` without
  addressing this first (same constraint `hosted/ap_exceptions` already
  documents for the same reason).
- **A key does not survive a process restart.** By design (see the
  threat model above) -- not a bug to fix later, a deliberate custody
  minimization.
- **The whole-request timeout wraps a streaming response's full
  duration**, including the time spent actually streaming tokens back
  to the client -- a very long real completion could be cut off at
  `COST_GATEWAY_REQUEST_TIMEOUT_SECONDS`. Raise the env var if this
  becomes a real problem before it's revisited properly.
- **No hosted dashboard yet.** `GET /v1/receipts` is the only way to
  read back a tenant's own data in Phase 1; the hosted dashboard (reusing
  the existing local dashboard, made multi-tenant) is Phase 2 scope.
- **No account/claim path yet.** A trial's data is genuinely gone once
  it expires or is ended -- there is no way to persist it beyond the TTL
  in Phase 1. That is Phase 4 scope (optional accounts).

## Testing

```bash
pytest tests/unit/hosted/test_cost_gateway_service.py -v
ruff check hosted/cost_gateway
mypy hosted/cost_gateway --strict --ignore-missing-imports
```
