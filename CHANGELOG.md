# Changelog

All notable changes to Inferrail are recorded here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versions
correspond to the milestones in `MISSION.md`, not necessarily to a new
PyPI release (the hosted service and website ship independently of the
`inferrail` package).

## v0.3.0 — in progress

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

### Notes

- v0.3.0 also includes a local control API, not yet built as of this
  entry — see `PROGRESS.md` for current status.

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
