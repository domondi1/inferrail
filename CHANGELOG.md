# Changelog

All notable changes to Inferrail are recorded here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versions
correspond to the milestones in `MISSION.md`, not necessarily to a new
PyPI release (the hosted service and website ship independently of the
`inferrail` package).

## v0.4.1 — 2026-09-15

### Added

- **A real opt-in, anonymous usage ping** (`src/inferrail/usage_ping/`,
  `docs/adr/0019-opt-in-usage-ping.md`), replacing v0.4.0's disabled
  Settings placeholder. Off by default, and inert with no
  `usage_ping.endpoint` configured — Inferrail ships with no built-in
  default endpoint. Four lifecycle events only, each sent at most once
  per install: `first_run`, `tool_connected`, `first_receipt`,
  `budget_created`. Never a prompt, response, model name, cost,
  work_id, project name, or anything about actual traffic. Sending
  never blocks, slows, or can fail the gateway — a background thread,
  short timeout, every failure swallowed silently.
- The dashboard's Settings toggle is now real
  (`GET`/`POST /v1/local/usage-ping`), and states "Not yet active — no
  collection endpoint is configured" whenever `usage_ping.endpoint` is
  unset, regardless of the toggle, so it never looks like it works when
  it can't.
- `inferrail telemetry preview|status|enable|disable` — `preview` in
  particular prints the exact JSON payload for every lifecycle event,
  from this install's real id/OS/version, without sending anything, so
  the ping's behavior is independently verifiable rather than trusted
  from documentation. Works standalone, without `--app-mode` or even an
  `inferrail.yaml`.
- `docs/privacy/usage-ping.md` — the plain-language privacy page linked
  from the Settings toggle, with the exact payload shown verbatim.
- **A proposed, built reference receiver** (`hosted/usage_ping/`): a
  small FastAPI service, own process, own SQLite storage, zero
  dependency on the `inferrail` package, no auth required to submit a
  ping (the payload is harmless and anonymous), a per-IP rate limit, a
  kill switch, and admin-token-gated aggregate `/stats`. **Never logs
  or persists the connecting IP address.** Deploying an instance and
  configuring `usage_ping.endpoint` to point at it is a human action —
  see `PROGRESS.md`'s "HUMAN ACTION NEEDED" for exact deploy steps.

## v0.4.0 — 2026-09-15

### Added

- The dashboard: a static React + Vite + TypeScript SPA in `app/`,
  served by `inferrail serve --app-mode` at `/dashboard` alongside the
  local control API. This unit ships the scaffold, real serving/auth,
  and one screen: **Live Feed**, streaming every receipt live over
  `GET /v1/local/stream`. See `docs/adr/0017-dashboard-in-app-directory.md`.
- `inferrail serve --app-mode` now prints a ready-to-open dashboard URL
  with the local API token already embedded (`?token=...`) — no
  additional terminal step to authenticate.
- The local control API's auth dependency now also accepts `?token=` as
  an alternative to the `Authorization` header, since browser
  `EventSource` cannot set custom headers.
- **Work** screen: cost per `work_id` over `GET /v1/local/work`, with a
  drill-down (`GET /v1/local/work/{work_id}`) at a real, hash-routed URL
  (`#/work/<id>`). A partially-priced work_id shows both its known total
  and how many receipts contributed nothing knowable (e.g. `$0.0007
  (+2 unknown)`), never a single misleading number.
- **Budgets** screen: create/remove budgets, a burn bar per budget over
  the new `GET /v1/local/budgets/spend` (reuses
  `budgets.enforcement.spent_so_far_usd` directly, the same computation
  `BudgetEnforcer.check` itself uses), and a blocked-request log. A
  pre-flight budget block now stamps the receipt's `attributes.budget_id`
  (`augment_attributes_with_block`), so the log is built from real
  evidence, not inferred from error text. `GET /v1/local/receipts` gained
  an optional `status` filter to support this.
- **Recover** screen: the pending human-review queue for AP
  invoice-exception decisions (new `GET /v1/local/ap/pending`, built
  from `ap.report.build_live_report` filtered to
  `awaiting_human_review`) with a one-click record-outcome (new
  `POST /v1/local/ap/{work_id}/outcome`, the same store call
  `inferrail ap outcome` makes). `inferrail serve --app-mode` now also
  provisions an AP recovery store at a fixed app-data path (printed on
  startup; override with `INFERRAIL_AP_DB`) — the first local-API
  surface to bridge to the previously separate `inferrail.ap` module.
- **Connect** screen: copy-paste snippets (curl, Claude Code/Anthropic
  SDK, the OpenAI Python SDK, LangChain) generated against
  `window.location.origin`, so they're correct for the exact running
  install rather than a generic placeholder host/port.
- **Settings** screen: real "Export" (new
  `GET /v1/local/receipts/export`, streams the same JSONL shape
  `inferrail receipts export` produces) and real pricing-catalog
  freshness (new `GET /v1/local/pricing/freshness`, reuses
  `cli.pricing.catalog_freshness` — never a network fetch). The
  "opt-in usage ping" control is a deliberately disabled placeholder:
  no telemetry-ping mechanism exists in this codebase, so the UI says
  so rather than pretending a checkbox does something.
- **All six MISSION.md v0.4.0 screens are now built.**
- The built dashboard is now bundled into the wheel this project's own
  CI builds — a new hatchling build hook (`hatch_build.py`) runs the
  dashboard build and packages it as `inferrail/dashboard_static/`.
  Never fails the build: a build environment without Node still
  produces a working, dashboard-less wheel, exactly as before. See
  `docs/adr/0018-dashboard-wheel-packaging.md`. Also fixed, found in the
  same pass: the sdist was including `app/node_modules` (real,
  pre-existing bloat, not something this pass introduced).

- `publish.yml` and `platform-verify.yml` now set up Node
  (`actions/setup-node@v4`) so the actual PyPI-published wheel and the
  three-OS `platform-verify.yml` wheels bundle the dashboard too, not
  just `ci.yml`'s own `dashboard` job — each workflow also now asserts
  the built/installed wheel actually contains
  `dashboard_static/index.html`, so a future Node/npm regression on any
  of those runners fails the run instead of silently shipping a
  dashboard-less release.
- **Fixed:** Live Feed never showed a receipt already in the store when
  the dashboard was opened — only ones sent *after* the tab connected —
  contradicting its own "every receipt this install has produced"
  subtitle. Found by opening the real dashboard in a real (headless
  Chromium) browser against a live `inferrail serve --app-mode`
  instance with existing receipts already on disk, not by reading the
  code. `listRecentReceipts` (`app/src/api.ts`) now seeds the screen
  from `GET /v1/local/receipts` (correctly reading the true tail, since
  that endpoint orders oldest-first) before the live SSE tail takes
  over.
- **Verified this session, end to end, with real clients — not just
  the existing test suite:** the real `openai` and `anthropic` Python
  SDKs, pointed at a live `inferrail serve --app-mode` process, both
  produce attributed receipts; a real `block`-mode budget genuinely
  rejects a request before the (mocked, since this session had no paid
  provider keys) upstream is ever called, and the block appears live in
  the dashboard's Budgets screen; no prompt/response text is persisted
  in any local store; the dashboard renders correctly in a real
  browser with zero console errors. See `PROGRESS.md`'s "v0.4.0 closing
  audit" section for the full record, including what this session could
  *not* verify (a real paid-provider round trip; streaming/tool-use
  against a real SDK).

### Not yet closed

- Whether the Settings screen's "opt-in usage ping" placeholder is
  accepted as final, or a real telemetry-ping feature gets scoped as
  its own future unit — a founder decision. **Resolved this session:
  build it for real** — see the new `## v0.4.1` section below (or
  `PROGRESS.md` if that unit hasn't landed yet).
- `GET /v1/local/receipts?limit=N` (with no `offset`) returns the
  *oldest* N receipts once an install has more than N total, not the
  most recent N — `ReceiptsStore.query()` always orders `ts` ascending.
  Worked around in Live Feed's own backfill (computed the correct
  `offset` explicitly) but the route itself has no "give me the most
  recent N" mode; a future caller could hit the same trap. Not fixed
  broadly this session — it's an existing, documented route contract
  change, out of scope for a closing-audit pass.
- Budget enforcement (and cost display generally) only ever applies
  when the (provider, model) pair has a *known* price — the built-in
  catalog (which requires the provider's real, unmodified `base_url`)
  or an explicit `pricing:` override. A budget scoped to an unrecognized
  model never blocks, by design (see `pricing/resolver.py`) — cost
  shows honestly as `unknown` rather than blocking on a guess, but this
  is easy to be surprised by. Documented here since this session's own
  first budget-enforcement test tripped on it.

## v0.3.0 — 2026-09-14

### Added

- WAL-mode SQLite receipts store (`receipts.sink: sqlite`), opt-in
  alongside the existing JSONL sink, indexed on
  `ts`/`work_id`/`project`/`model`. `inferrail report`/`transaction`/
  `work` work unchanged against either sink (auto-detected). New
  `inferrail receipts import|export` moves history between them. See
  `docs/adr/0013-sqlite-receipts-store.md`.
- Anthropic-compatible `POST /v1/messages` passthrough — a genuinely
  separate, wire-native pipeline (not a translation of
  `/v1/chat/completions`), with real streaming, tool use, and pricing
  via a new, independently-verified Anthropic catalog. This is what
  makes pointing Claude Code (or any Anthropic SDK client) at Inferrail
  work. See `docs/adr/0014-anthropic-messages-passthrough.md`.
- Real budget enforcement (opt-in, `budgets.enabled: true`, requires
  `receipts.sink: sqlite`): `global`/`project`/`work_id`-scoped spend
  caps over a `per_work`/`daily`/`monthly` window, in `warn` or `block`
  mode. A `block` budget rejects a request with HTTP 402 (machine-
  readable `error.details`) before any provider is contacted, using a
  catalog-based upper-bound estimate; the block is still recorded as a
  normal receipt. A `warn` budget never blocks, but a real overrun is
  recorded on the receipt as `budget_overrun_usd`. New CLI: `inferrail
  budget set|list|rm`. Shared between `/v1/chat/completions` and
  `/v1/messages`. See `docs/adr/0015-budget-enforcement.md`.
- `inferrail serve --app-mode`: relocates receipts/budgets under the OS
  app-data directory (forcing `receipts.sink: sqlite` and
  `budgets.enabled: true`) and mounts a local control API
  (`/v1/local/receipts|work|budgets|stream`) guarded by a mandatory
  per-install token — for the not-yet-built desktop dashboard, not a
  hosted/cross-fleet capability. New `inferrail pricing update`
  (reports built-in catalog freshness; never fetches over the network)
  and `inferrail doctor` (port, pricing freshness, provider
  reachability — one-line fixes). See
  `docs/adr/0016-local-control-api.md`.

This closes all four v0.3.0 units from `MISSION.md`.

## v0.2.1 — 2026-09-14

### Added

- Self-serve sandbox tenancy for the hosted AP Exceptions service:
  `POST /v1/sandbox` (no auth) issues a short-lived, isolated,
  synthetic-data-only API key with no account and no human in the loop.
  See `docs/adr/0012-self-serve-sandbox-tenancy.md`.
- Abuse guards on the new route: a per-IP issuance throttle, a global
  live-tenant ceiling, a kill switch (`AP_SANDBOX_ENABLED`), and a
  request-size limit applied to every route.
- Every response from a sandbox tenant is explicitly labeled
  (`"sandbox": true` + `sandbox_notice`); operator-tenant responses now
  explicitly carry `"sandbox": false`.
- A four-command copy-paste walkthrough (get key → create decision →
  record retry attempt → read report) published in
  `hosted/ap_exceptions/README.md` and on the website
  (`docs/index.html`), replacing the prior "visitors can only reach
  `/health`" messaging.
- `MISSION.md` and `PROGRESS.md` — the durable multi-session brief and
  the fast-moving state tracker this changelog entry itself follows.

### Notes

- Hosted-service and website change only; no `inferrail` PyPI package
  change, so `pyproject.toml`'s version is unchanged at `0.2.0`.
