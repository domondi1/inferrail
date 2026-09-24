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

**Operating it:** see [`LAUNCH.md`](LAUNCH.md) for the security review
(what protects visitors, and which test checks each item), the launch
checklist, and an incident runbook.

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
| `COST_GATEWAY_CLIENT_IP_HEADER` | `true-client-ip` | Header the per-IP throttle reads the real client address from. `True-Client-IP` is set by Cloudflare, which fronts every Render service, and overwrites any client-supplied value. `X-Forwarded-For` is deliberately not used: Render appends to it without clearing a client-supplied value, so it's spoofable. Falls back to the socket peer address when the header is absent. **Set to empty if deploying somewhere not behind Cloudflare**, or a client could set this header itself. `/v1/admin/stats` → `trial_issuance_ip_source` shows which source was used, so you can confirm it in production. |
| `COST_GATEWAY_PURGE_GRACE_SECONDS` / `COST_GATEWAY_PURGE_INTERVAL_SECONDS` | `300` / `60` | Expired-tenant purge grace period and background sweep interval. |
| `COST_GATEWAY_RATE_LIMIT_MAX_REQUESTS` / `COST_GATEWAY_RATE_LIMIT_WINDOW_SECONDS` | `60` / `60` | Per-tenant request-rate limit across every authenticated route. |
| `COST_GATEWAY_DAILY_BUDGET_USD` | `1.00` | Each tenant's own daily spend cap, block mode -- enforced pre-flight before any real provider is ever called, via the same `BudgetEnforcer` `inferrail serve`'s budgets use. |
| `COST_GATEWAY_REQUEST_TIMEOUT_SECONDS` | `120` | Whole-request timeout, including a streaming response's full duration -- see "Known limitations" below. |
| `COST_GATEWAY_MAX_REQUEST_BODY_BYTES` | `262144` (256 KiB) | Request body size guard, applied to every route including the unauthenticated `POST /v1/trial`. Counts bytes actually received, so a chunked upload with no `Content-Length` is capped too. |
| `COST_GATEWAY_CORS_ORIGINS` | `*` | Comma-separated allowed origins for browser CORS (Phase 2's website calls this API directly from a browser). `*` is safe here because every route is bearer-token-gated, not cookie-authenticated -- see `service.py`'s `_cors_origins_from_env`. Narrow this for a production deployment if desired. |
| `COST_GATEWAY_ADMIN_TOKEN` | unset | Enables `GET /v1/admin/stats` and `GET /v1/admin/feedback` (usage counters + submitted feedback), gated on `Authorization: Bearer <this value>`. **Unset by default -- both routes return `404` (not `401`) until this is explicitly set**, so a deployment with no admin token configured reveals nothing about their existence. Generate a real secret yourself (e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`) and set it as a platform secret; never commit it or share it with an assistant session. |
| `COST_GATEWAY_GITHUB_TOKEN` | unset | A fine-grained GitHub PAT, scoped to **only** `COST_GATEWAY_GITHUB_REPO` with **Issues: write** permission and nothing else. When set (together with `COST_GATEWAY_GITHUB_REPO`), every `POST /v1/feedback` also files a GitHub Issue -- the durable copy, since it survives a Render redeploy and the local `feedback.jsonl` doesn't. Unset by default (feedback is still saved locally either way). See "Feedback and usage visibility" below for how to generate one. |
| `COST_GATEWAY_GITHUB_REPO` | unset | Which repo feedback Issues are filed on, as `owner/repo`. **Must be a private repository** -- the service checks this via the GitHub API before every filing (until confirmed once) and files nothing to a public repo. No default: nothing is filed unless this is set. |
| `COST_GATEWAY_FEEDBACK_MAX_PER_TENANT` | `5` | Spam guard: max feedback submissions per trial; further ones get `429`. |
| `COST_GATEWAY_FEEDBACK_ISSUES_MAX_PER_HOUR` | `20` | Global cap on GitHub Issues filed per rolling hour. Past it, feedback is still accepted and saved locally; only the GitHub copy is skipped. |

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

# 7. Declare an outcome for a unit of work (send step 5's request with an
#    extra "X-Inferrail-Attribute-Work-Id: wid_1" header first).
curl -s -X POST http://127.0.0.1:8423/v1/work/wid_1/outcome \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"outcome_status": "resolved"}'

# 8. Read back that unit of work's economics -- receipts joined with the
#    declared outcome, same shape `inferrail work wid_1` prints locally.
curl -s http://127.0.0.1:8423/v1/work/wid_1 -H "Authorization: Bearer $API_KEY"

# 9. Every known unit of work for this trial (`inferrail work --all`).
curl -s http://127.0.0.1:8423/v1/work -H "Authorization: Bearer $API_KEY"

# 10. Group receipts by any attribute you attached (`inferrail report --by`).
curl -s "http://127.0.0.1:8423/v1/report?by=customer" -H "Authorization: Bearer $API_KEY"

# 11. Every receipt sharing one X-Inferrail-Attribute-Task-Id value,
#     aggregated (`inferrail transaction <task_id>`).
curl -s http://127.0.0.1:8423/v1/transaction/task_1 -H "Authorization: Bearer $API_KEY"

# 12. Forget your key without ending the trial.
curl -s -X DELETE http://127.0.0.1:8423/v1/trial/$TENANT_ID/keys -H "Authorization: Bearer $API_KEY"

# 13. End the trial entirely, right now.
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

## Parity with the CLI (Phase 3)

The design goal, stated plainly: a client pointed at `inferrail serve
--quickstart` and a client pointed at a hosted trial's `base_url` should
behave identically, with only `base_url` changed. This is true **by
construction**, not by coincidence -- this service never reimplements
wire-format, routing, receipt, or aggregation logic; it constructs the
same classes the CLI's own gateway builds
(`gateway.execution.InferenceEngine`, `gateway.anthropic_execution.
AnthropicInferenceEngine`, `providers.openai.OpenAIProvider`,
`providers.anthropic.AnthropicProvider`, `routing.router.Router`) and
calls the same aggregation functions the CLI's own commands call
(`work.builder.build_work_summary`/`aggregate_work_summaries`,
`transactions.builder.build_transaction`,
`receipts.aggregation.summarize_receipts`). See
`tests/unit/hosted/test_cost_gateway_service.py`'s
`test_hosted_and_local_gateway_produce_structurally_equivalent_results`
for the automated proof: the identical request, sent through a local
`InferenceEngine` and through this service's `/v1/chat/completions`
against the identical mocked upstream, produces the same response
content/usage and the same receipt shape.

**Confirmed identical:**

- `/v1/chat/completions` and `/v1/messages`: full OpenAI-/Anthropic-
  compatible passthrough, including streaming (`"stream": true`, real
  SSE passthrough, not buffered) and tool calling (tool-call arguments
  preserved byte-exact, never reparsed -- see
  `test_real_chat_completions_tool_call_passthrough_byte_exact`).
  `model` is forwarded verbatim (docs/adr/0007), exactly like
  `inferrail serve`'s own passthrough default.
- `X-Inferrail-Attribute-<Name>` headers: same extraction function
  (`gateway.attribution.extract_attributes`), same persistence onto the
  receipt's `attributes` field, never forwarded upstream.
- `InferenceReceipt` schema: identical fields, identical payload-free
  guarantee (structural, not policy) -- see `test_demo_receipt_appears_
  in_receipts_listing`'s explicit field-absence assertions.
- Work Economics (`GET /v1/work[/​{work_id}]`, `POST /v1/work/{work_id}/
  outcome`), task transactions (`GET /v1/transaction/{task_id}`), and
  per-attribute reports (`GET /v1/report?by=<attribute>`): same
  aggregation primitives as `inferrail work`/`inferrail transaction`/
  `inferrail report`, added this phase.
- Budgets: pre-flight, catalog-based, block-before-provider-call
  enforcement via the same `BudgetEnforcer` -- present since Phase 1.

**Differences, minimized and stated explicitly (never silent):**

- **Outcome storage:** the CLI's `inferrail work outcome` appends to a
  JSONL file you choose; this service appends to a JSONL file scoped to
  your tenant automatically (`tenant_store.py`) -- same format, same
  `WorkOutcomeRecord`/`append_outcome`, different (automatic) file
  selection only.
- **No local control API / dashboard parity yet.** The CLI's `--app-mode`
  local control API (docs/adr/0016) has a paginated/SSE-tail shape this
  service doesn't mirror -- `GET /v1/receipts` here is a simpler
  limit/offset list. A closer-parity hosted dashboard is later scope.
- **No MCP server.** `inferrail-mcp`'s `get_spend`/`get_health` are a
  local stdio process reading a local receipts file by design -- this
  codebase has no remote/HTTP MCP transport to expose, and building one
  is a materially different, unauthorized-so-far capability, not a
  small addition. A trial user who wants MCP access today can export
  their own receipts (`GET /v1/receipts`) and point the existing local
  MCP server at the exported file.
- **Rate limits and a daily budget cap exist here and don't exist by
  default in a bare `inferrail serve`** -- an intentional, documented
  trial-safety addition (Phase 1), not a passthrough-fidelity gap.

## How a frontend would call this (Phase 2 preview)

Phase 2 builds the actual one-click website flow. The contract above is
already everything it needs: `POST /v1/trial` on the "Try Free" click,
immediately show `base_url` + a "send a test request" button wired to
`POST /v1/demo/chat/completions`, a password-style input wired to
`POST /v1/trial/{tenant_id}/keys` for the optional real-key step, and a
live countdown driven by polling `GET /v1/trial/{tenant_id}` (or, in a
later phase, a push-based equivalent) for `seconds_remaining`.

## Feedback and usage visibility (founder-facing)

Every trial-authenticated visitor can `POST /v1/feedback` (free-text
`message`, optional `contact`) -- surfaced in the "Try Free" page as a
plain "Report an issue" form. Feedback is written to two places:

1. **`feedback.jsonl` in `COST_GATEWAY_DATA_DIR`** -- always, keyed by
   the reporting tenant, **not deleted when that tenant's trial ends or
   expires.** On Render's free tier this file lives on ephemeral
   storage, though -- **it does not survive a redeploy/restart.**
2. **A GitHub Issue on `COST_GATEWAY_GITHUB_REPO`**, labeled
   `cost-gateway-feedback` -- best-effort, only if both
   `COST_GATEWAY_GITHUB_TOKEN` and `COST_GATEWAY_GITHUB_REPO` are set.
   This is the durable copy: it survives every redeploy, since GitHub --
   not Render -- holds it. If the GitHub call fails for any reason
   (missing/bad token, GitHub unreachable, hourly cap reached), the
   feedback is still saved to (1) and the submission still succeeds --
   see `_GitHubFeedbackSink`'s own docstring.

**Feedback only ever goes to a private repository.** It can contain a
visitor's email address and whatever they chose to write; someone
filling in a form on a website hasn't agreed to publish that on a public
issue tracker. The service checks that `COST_GATEWAY_GITHUB_REPO` is
private (via the GitHub API) before filing, and files nothing if it
isn't. If a report describes a real bug worth tracking in the open, the
operator opens a separate, cleaned-up public issue by hand.

**Setting up the GitHub Issues path** (recommended -- this is the
durable one):

1. Create a **private** repository to receive feedback -- a dedicated
   one, holding nothing else, so the token below can't reach anything
   sensitive.
2. Create a **fine-grained personal access token** at
   github.com/settings/personal-access-tokens/new, scoped to **only**
   that repository, with **Issues: Read and write** permission and
   nothing else (GitHub adds read-only "Metadata" automatically; the
   service uses it for the private-repo check). Never a classic PAT with
   broad repo access for this -- narrow scope limits what a leaked token
   could do.
3. Set `COST_GATEWAY_GITHUB_REPO` (`owner/repo`) and
   `COST_GATEWAY_GITHUB_TOKEN` on Render (same process as
   `COST_GATEWAY_ADMIN_TOKEN` above), redeploy.
4. From then on, new feedback appears as a normal GitHub Issue in that
   private repo, labeled `cost-gateway-feedback`. No curl command needed
   for this path.

**The Render-side admin view (`/v1/admin/feedback`) still works exactly
as before**, and is useful for whatever's arrived since the last
redeploy even if you haven't set up the GitHub token. Set
`COST_GATEWAY_ADMIN_TOKEN` (see the env var table above), then:

```bash
curl -s https://<your-deployment>/v1/admin/feedback -H "Authorization: Bearer <admin-token>"
curl -s https://<your-deployment>/v1/admin/stats -H "Authorization: Bearer <admin-token>"
```

`/v1/admin/stats` reports `trials_issued_total` and `trials_live_now`.
**Read honestly: this counts trials issued, not unique people** -- there
is no account system yet (that's Phase 4 scope), so nothing here can
tell one visitor starting two trials apart from two different visitors.
Both counters are **process-local and reset on every restart/redeploy**
-- not a durable historical record. If you want a real, persistent
usage history across redeploys, that needs a small durable store (a
SQLite counter file, same pattern as everything else in this service)
-- not built yet; flagged here as the natural next step if this number
starts mattering for real decisions.

## Logs (what's recorded, and what never is)

The service writes one JSON object per line to stdout (Render's **Logs**
tab shows them; any log drain can filter on the fields). Every field
comes from a fixed allow-list in `service.py` (`_LOG_FIELDS`) --
**key-free and payload-free by structure**: no line is ever built from a
request's headers, body, URL, or an exception's message, so provider
keys, trial tokens, prompts, responses, and feedback text/emails have no
path into a log. `test_logs_never_contain_secrets_or_payloads` checks
this end to end.

| `event` | When | Fields |
|---|---|---|
| `startup` | Process start | `admin_enabled`, `github_feedback_configured`, `client_ip_header` |
| `request` | Every request, including 413/504 rejections -- except successful `/health` checks, which the host's health checker sends every few seconds (a failing one is logged) | `request_id`, `method`, `route` (the route *template*, e.g. `/v1/trial/{tenant_id}`, or `unmatched`), `status`, `duration_ms`, `tenant_id` |
| `unhandled_error` | A bug raised an exception | `request_id`, `method`, `route`, `error_type`, `error_location` (`file.py:line`) -- never the exception message |
| `purge` / `purge_error` | The background sweep removed expired trials / a sweep failed (the loop keeps running) | `purged_count` / `error_type`, `error_location` |
| `feedback_github_skipped` | Feedback wasn't copied to GitHub | `reason` (`hourly_cap`, `repo_check_failed`, `repo_not_private`, `issue_create_failed`, `request_error:<type>`), `http_status` |

Every response carries an **`X-Request-ID`** header matching its
`request` log line, and a 500 from an unhandled error returns it in the
body too -- so a visitor's bug report can be matched to the exact log
line.

**Useful searches in Render's log viewer:** `"unhandled_error"` (bugs),
`"feedback_github_skipped"` (feedback not reaching the private repo, with
the reason), `"status": 5` (server errors), or a specific `request_id`.

## Known Phase 1 limitations, documented rather than hidden

- **Single-instance only.** In-memory trial/rate-limit state
  (`trial.TrialRegistry`, `auth.RateLimiter`, `keys.KeyVault`) is
  process-local -- multiple instances behind a load balancer would each
  have their own uncoordinated view. Do not run more than one instance
  of this service against the same `COST_GATEWAY_DATA_DIR` without
  addressing this first (same constraint `hosted/ap_exceptions` already
  documents for the same reason).
- **In-memory abuse-guard tables are bounded by live state, not by
  history.** A purged trial's rate-limit entry is dropped with it, and
  per-IP issuance history is pruned once it falls outside the issuance
  window -- so memory tracks current traffic, not every visitor ever
  seen.
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
