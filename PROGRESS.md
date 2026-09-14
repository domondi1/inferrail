# PROGRESS.md — fast-moving state tracker

Read this after `MISSION.md` every session. This file changes every
session; `MISSION.md` almost never does.

## Current milestone

**v0.2.1 — "Visitors can run the hosted workflow themselves"** (see
`MISSION.md`). Status: **code merged to `main`
([PR #20](https://github.com/domondi1/inferrail/pull/20), founder-
reviewed and merged 2026-09-14, merge commit `ab4eb54`) — but the live
Render instance at `https://inferrail-ap-exceptions.onrender.com` has
NOT yet redeployed from it as of this session's last check. This
milestone's acceptance criterion ("from a machine with only curl, a
person with no prior context completes the four commands") is not yet
true in production. See "HUMAN ACTION NEEDED" and "Next session starts
here."**

**Why the harness blocked the earlier merge attempt (now moot, kept for
context):** the coding harness's own safety classifier refused an
agent-initiated `gh pr merge --squash` with "Permission for this action
was denied ... [Merge Without Review]" — a session-level tool gate, not
GitHub or this repo's branch protection. The founder reviewed and
merged it directly instead.

### Checklist

- [x] `POST /v1/sandbox` (no auth): issues `{api_key, tenant_id,
      expires_at, ttl_seconds, max_rows_per_tenant, rate_limit}`.
      (`hosted/ap_exceptions/sandbox.py`, wired in `service.py`.)
- [x] Sandbox tenant authenticates identically to an operator tenant
      (same `_tenant_store` dependency, same per-tenant SQLite
      isolation) — no parallel decision/retry/report code path.
- [x] Expiry: an expired sandbox key gets an explicit `401` naming the
      expiry time and pointing back at `POST /v1/sandbox` (distinct
      from the generic "invalid API key" an operator gets).
- [x] Row cap: `AP_SANDBOX_MAX_ROWS_PER_TENANT` (default 20) blocks a
      *new* `work_id` with `429`; an idempotent replay of an existing
      `work_id` is never blocked.
- [x] Abuse guards, all independently configurable and independently
      tested: per-IP issuance throttle
      (`AP_SANDBOX_ISSUE_MAX_PER_IP`/`_WINDOW_SECONDS`), global live-
      tenant ceiling (`AP_SANDBOX_MAX_LIVE_TENANTS`), kill switch
      (`AP_SANDBOX_ENABLED=false`), global request-size limit
      (`AP_MAX_REQUEST_BODY_BYTES`, applies to every route including
      the unauthenticated one).
- [x] Auto-purge: an expired sandbox tenant's SQLite file is deleted
      (`TenantStoreRegistry.purge_tenant`) both lazily (next
      issuance/lookup) and via a periodic background sweep
      (`AP_SANDBOX_PURGE_INTERVAL_SECONDS`, default 60s) that runs even
      with no further traffic.
- [x] Every response is explicitly labeled `"sandbox": true/false` (+
      `sandbox_notice` when true) — `_stamp()` in `service.py`, applied
      to all nine JSON-returning routes.
- [x] Four-command copy-paste walkthrough (get key → create decision →
      record retry attempt → read report) in
      `hosted/ap_exceptions/README.md` ("Try it yourself" section) and
      on the website (`docs/index.html`, `#ap-hosted` section) —
      replaces the old "visitors can only reach `/health`" copy.
- [x] Tests: issuance shape, full walkthrough end-to-end, sandbox vs.
      operator stamping, cross-tenant isolation (sandbox/sandbox and
      sandbox/operator), expiry error message, per-IP issuance
      throttle, global ceiling, kill switch, row cap (including that an
      idempotent replay is exempt), request-size limit, and that a
      sandbox key is never confused with an operator key or vice versa.
      All in `tests/unit/hosted/test_ap_exceptions_service.py`.
- [x] `docs/adr/0012-self-serve-sandbox-tenancy.md` — the architectural
      decision record.
- [x] `docs/PRODUCT.md`'s "Hosted API (optional)" bullet updated to
      mention self-serve sandbox issuance.
- [x] `CHANGELOG.md` created (didn't exist before) with a v0.2.1 entry.
- [x] `MISSION.md` (this multi-session brief, verbatim from the
      founder's instructions) and this file created at repo root.
- [x] Open the PR, get it through CI, get it merged. **Done:** PR #20
      merged to `main` 2026-09-14 (merge commit `ab4eb54`), founder-
      reviewed.
- [ ] **Still not true in production as of this session:** the
      acceptance criterion "from a machine with only curl, a person
      with no prior context completes the four commands successfully"
      against the actual `https://inferrail-ap-exceptions.onrender.com`
      deployment. Verified directly this session: `GET /health` returns
      `200 {"status": "ok"}` (service is up), but `POST /v1/sandbox`
      returns `404 {"detail": "Not Found"}` and the live instance's own
      `GET /openapi.json` still lists only the pre-merge route set
      (`/health`, `/v1/decisions`, `/v1/decisions/{work_id}`, `.../handoff`,
      `.../outcome`, `.../reap`, `.../retry-attempts`, `/v1/reap-stale`,
      `/v1/report` — no `/v1/sandbox`). Polled for ~6 minutes after the
      merge with no change. **Render has not redeployed from the new
      `main` yet** — see "HUMAN ACTION NEEDED." Do not mark v0.2.1 fully
      done until this route is confirmed live.

### Local verification performed this session

```
ruff check .                                                    # pass
mypy                                                             # pass (62 files)
mypy hosted/ap_exceptions --strict --ignore-missing-imports      # pass (matches CI job)
ruff check src/inferrail/ap hosted/ap_exceptions examples \
  tests/unit/ap tests/unit/hosted/test_ap_exceptions_service.py  # pass (matches CI job)
pytest tests/unit/ap tests/unit/hosted/test_ap_exceptions_service.py  # 129 passed (matches CI job)
```

Full-repo `pytest -q` was also run to completion this session: **708
passed, 19 skipped, 0 failed** (the 19 skips are the pre-existing
credential-gated integration tests, unrelated to this change).

PR #20 opened from `feat/hosted-ap-self-serve-sandbox` to `main`; all 8
CI checks (`test` 3.11/3.12, `wheel-smoke` macos/ubuntu/windows,
`ap-exceptions`, `hosted-economic-authority`, `boundary-check`) came
back green. **Merge itself was refused by the coding harness's own
safety classifier** ("Merge Without Review"), not by GitHub or this
repo's branch protection — see "Why not merged already" above.

## Next session starts here

PR #20 is merged. The only remaining step for v0.2.1 is confirming the
live Render deployment actually serves the new code.

1. Recheck the live instance:
   `curl -s -X POST https://inferrail-ap-exceptions.onrender.com/v1/sandbox`.
   - If it now returns `{"api_key": "sbx_...", ...}`: run the full
     four-command walkthrough for real against the live URL (not just
     `TestClient`), confirm step 4's report shows
     `observed_cost_complete: true`, check off the remaining checklist
     item above, mark v0.2.1 fully done in this file, and move to
     v0.3.0's first unit (SQLite receipts store — see `MISSION.md`).
   - If it still 404s: this is not something the agent can fix from
     this repo (no `render.yaml`/deploy-hook lives here — see
     `hosted/ap_exceptions/README.md`'s "Deploying it" section; Render
     project configuration is external, referenced from the private
     repo's `ops/deployments.md`). Don't keep polling indefinitely each
     session — ask the founder whether Render's auto-deploy is enabled
     for this service and pointed at `main`, or whether a manual deploy
     needs triggering from the Render dashboard. In the meantime, this
     does not block starting v0.3.0's first unit.
2. Once live-verified, also sanity-check that `GET /health` on the live
   instance still returns `200` after whatever redeploy happened (a
   basic regression check, not specific to this feature).

## HUMAN ACTION NEEDED

- **Render has not redeployed `hosted/ap_exceptions` from the merged
  `main` (merge commit `ab4eb54`) as of this session's last check.**
  `GET /health` on `https://inferrail-ap-exceptions.onrender.com` is up
  and returns `200`, but `POST /v1/sandbox` still 404s and the live
  instance's own `GET /openapi.json` lists only the pre-merge route
  set. The agent has no Render dashboard/API access to trigger or
  inspect a deploy from here. Please check whether auto-deploy is
  enabled and pointed at `main` for this service, or trigger a manual
  deploy — then a future session (or you) can re-run
  `curl -s -X POST https://inferrail-ap-exceptions.onrender.com/v1/sandbox`
  to confirm.
- **Render warm/upgrade decision.** The hosted AP Exceptions demo
  (`https://inferrail-ap-exceptions.onrender.com`) runs on Render's free
  tier: no persistent disk (irrelevant to sandbox tenants, which are
  meant to be ephemeral, but still true for operator tenants) and it
  spins down when idle (cold start up to ~1 minute+, no upper bound).
  The walkthrough documents this honestly rather than hiding it. If you
  want a snappier first impression for visitors, upgrading to a paid
  Render plan (persistent disk + no idle spin-down) is a paid-account
  decision only you can make — not blocking v0.2.1, since the docs are
  honest about the cold start either way.
- **No other new human action this session.** Signing accounts,
  stopwatch tests, demo video/screenshots, and HN post timing remain
  deferred per `MISSION.md`'s standing ledger — untouched this session.

## Decisions made this session

- **Sandbox tenant = ordinary tenant, not a parallel code path.** A
  sandbox key authenticates through the same dependency
  (`_tenant_store`) and hits the same `RecoveryStore`/`recommend()`
  logic as an `AP_API_KEYS` tenant. Rationale: MISSION.md's honesty
  requirement — a "demo" workflow that secretly runs different logic
  than the real one would misrepresent what a visitor is actually
  seeing. See `docs/adr/0012`.
- **Sandbox metadata is in-memory only, not a second persistent store.**
  Matches the existing precedent `auth.RateLimiter` already set in this
  same service. Trade-off: a sandbox key stops working across a process
  restart (visitor just requests a new one — acceptable for a few
  minutes of exploration). See `docs/adr/0012`'s "Consequences."
  Per-tenant *data* (the SQLite file) still persists across a restart
  until actually purged by TTL + grace period.
- **Four-command walkthrough uses a manual `export API_KEY=...` paste
  step between commands 1 and 2**, rather than a one-liner that
  auto-parses JSON with `jq`/`python3`. Rationale: don't assume `jq` is
  installed on a machine with "only curl" (the acceptance criterion's
  own wording); a human reading and pasting one value is still
  genuinely copy-paste and doesn't compromise the "no human in the
  loop" issuance requirement (that requirement is about Inferrail's
  side of issuance, not the visitor's own terminal actions).
- **Kept the milestone's literal four-command list** (get key → create
  decision → record retry attempt → read report) rather than also
  adding a fifth "record an outcome" command that appears in
  `MISSION.md`'s looser End-state-2 prose — the operational milestone
  bullet is more precise and explicitly says "four curl commands"; the
  outcome-recording endpoint already exists and works identically for a
  sandbox tenant if a future session wants to extend the walkthrough.
- **Did not bump `pyproject.toml`'s version.** This milestone touches
  only `hosted/` and `docs/`/website — zero `src/inferrail` changes — so
  there is nothing new to publish to PyPI. Added `CHANGELOG.md` (new
  file) to record the milestone anyway, per `MISSION.md`'s "leave it
  releasable" rule.
- **Added a global request-size-limit middleware**, not a sandbox-only
  one — an unauthenticated `POST /v1/sandbox` is exactly the route an
  attacker would target with an oversized body, but there's no reason
  operator routes should be unprotected either.

## Reference: where things live

- Sandbox issuance/validation logic: `hosted/ap_exceptions/sandbox.py`
- Wiring into the service (auth, routes, stamping, row cap, background
  purge, request-size middleware): `hosted/ap_exceptions/service.py`
- Tenant file deletion on purge: `hosted/ap_exceptions/tenant_store.py`
  (`purge_tenant`)
- `RateLimiter.max_requests`/`.window_seconds` public properties added
  to `hosted/ap_exceptions/auth.py` (previously private-only; needed so
  `POST /v1/sandbox`'s response body can report the rate limit without
  reaching into a private attribute)
- Tests: `tests/unit/hosted/test_ap_exceptions_service.py` (search
  `sandbox` for the new ones; two pre-existing exact-dict-equality
  assertions in `test_reap_endpoint_transitions_a_stale_decision` were
  loosened to field-level checks since every response now carries the
  new `sandbox`/`sandbox_notice` fields)
- ADR: `docs/adr/0012-self-serve-sandbox-tenancy.md`
- Docs touched: `hosted/ap_exceptions/README.md`, `docs/PRODUCT.md`,
  `docs/index.html` (`#ap-hosted` section + generalized the page's
  copy-button JS to wire every `.install .copy` button, not just the
  first one in the DOM — needed once more than one existed)
