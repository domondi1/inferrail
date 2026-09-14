# 0012. Self-serve sandbox tenancy for the hosted AP Exceptions service

## Status

Accepted

## Context

Before this change, `hosted/ap_exceptions` had exactly one way to get a
tenant: an operator adds a key to `AP_API_KEYS`. A visitor to the hosted
demo instance with no key could reach `GET /health` and nothing else —
confirming the process was up, never the actual decide → retry →
report workflow (`README.md`'s own prior wording said so explicitly).
`MISSION.md`'s End-state 2 requires closing exactly this gap: any
visitor, with no account and no human in the loop, must be able to
obtain a credential and run the full hosted workflow themselves, from a
machine with only `curl`.

This requires an unauthenticated, publicly-reachable issuance route —
which is new attack surface a bounded-issuance operator flow never had.
The design has to answer: how is a self-issued tenant kept from
threatening the same properties `AP_API_KEYS` tenants already have
(isolation, honest labeling, bounded resource use), and how is the
issuance route itself kept from being an unbounded resource sink or a
vector for spamming/abusing the underlying decision engine.

## Decision

A sandbox tenant is an ordinary tenant, not a separate code path: it
authenticates via the exact same `Authorization: Bearer <api-key>`
header, gets the exact same per-tenant SQLite isolation
(`tenant_store.TenantStoreRegistry`), and exercises the exact same
`inferrail.ap.policy.recommend` / `RecoveryStore` logic as an
operator-provisioned tenant. `service.py`'s `_authenticate` dependency
checks a new `sandbox.SandboxRegistry` first (a `sbx_`-prefixed key can
never collide with an operator-chosen key), then falls back to the
existing `auth.authenticate` for everything else — so every existing
route, test, and guarantee about operator tenants is untouched.

What actually differs, all enforced in `sandbox.py` and `service.py`:

- **Bounded lifetime.** A sandbox key is minted with `expires_at`
  (`AP_SANDBOX_TTL_SECONDS` from issuance). An expired key gets an
  explicit `401` naming the expiry time and pointing back at
  `POST /v1/sandbox` — never the same generic "invalid API key" an
  operator gets for a wrong key, because a visitor who just watched
  their key work needs to know *why* it stopped, not just that it did.
- **Bounded size.** `SANDBOX_MAX_ROWS_PER_TENANT` caps distinct
  `work_id`s per sandbox tenant; an idempotent replay of an existing
  `work_id` is never blocked by this (it isn't a new row) — consistent
  with every other idempotency guarantee this service already makes.
- **Bounded issuance.** Four independent guards, all separately
  configurable and independently testable: a per-IP issuance throttle
  (`AP_SANDBOX_ISSUE_MAX_PER_IP` / `_WINDOW_SECONDS`), a global ceiling
  on live sandbox tenants (`AP_SANDBOX_MAX_LIVE_TENANTS`), a kill switch
  (`AP_SANDBOX_ENABLED=false`), and a global request-size limit
  (`AP_MAX_REQUEST_BODY_BYTES`) that protects the unauthenticated
  issuance route the same way it protects every other route.
- **Explicit labeling.** Every response from a sandbox tenant carries
  `"sandbox": true` and a `sandbox_notice` string (`_stamp` in
  `service.py`) — an operator tenant's responses carry `"sandbox":
  false` and `"sandbox_notice": null`, so the field is never silently
  absent either way.
- **Automatic purging.** Sandbox metadata (which key hashes to which
  tenant, its expiry, its issuing IP) lives in memory only — the same
  choice `auth.RateLimiter` already made for the same reason: it avoids
  a second persistent store next to `tenant_store.py`'s per-tenant
  SQLite files, at the acceptable cost of a sandbox key needing
  re-issuance across a process restart. An expired tenant's underlying
  SQLite file is deleted (`TenantStoreRegistry.purge_tenant`) once
  `AP_SANDBOX_PURGE_GRACE_SECONDS` past expiry, both lazily (on the next
  issuance or lookup) and via a periodic background sweep
  (`AP_SANDBOX_PURGE_INTERVAL_SECONDS`) so an abandoned tenant is
  reclaimed even with no further traffic naming it.

## Consequences

- Every acceptance criterion in `MISSION.md`'s v0.2.1 is met without a
  parallel, harder-to-audit implementation of decision/retry/report
  logic — a sandbox tenant runs the identical code path an operator
  tenant does, just capped and time-boxed.
- The unauthenticated `POST /v1/sandbox` route is the one place this
  service accepts a request with no proof of prior relationship at all;
  it is deliberately the most heavily-guarded route in the service (four
  independent limits) precisely because of that.
- A future hosted capability that wants the same "try it with no
  account" property can reuse this shape (an unauthenticated issuance
  route producing an ordinary, bounded, expiring tenant) rather than
  inventing a separate demo-mode code path.
- Sandbox tenant state does not survive a process restart. This is
  acceptable for a bounded-lifetime credential meant for a few minutes
  of exploration, not for anything a visitor should rely on afterward —
  the walkthrough and every response say so explicitly.
