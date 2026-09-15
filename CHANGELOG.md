# Changelog

All notable changes to Inferrail are recorded here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versions
correspond to the milestones in `MISSION.md`, not necessarily to a new
PyPI release (the hosted service and website ship independently of the
`inferrail` package).

## v0.4.0 — in progress

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

### Not yet in this milestone

- The Connect and Settings screens (visible in the nav as disabled
  tabs, not omitted).
- Bundling the built dashboard into the PyPI wheel — build it from a
  checkout (`cd app && npm install && npm run build`) until a packaging
  unit lands.

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
