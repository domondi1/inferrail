# 0016. `inferrail serve --app-mode`: a local control API, an app-data directory, and diagnostics

## Status

Accepted

## Context

`MISSION.md`'s v0.3.0 calls for the last of its four units: "a local
control API on `127.0.0.1` with a per-install token: receipts query
(paginated), work rollups, budgets CRUD, `GET /v1/local/stream` (SSE of
new receipts). New `inferrail serve --app-mode` enables sqlite +
control API + OS app-data dir", plus `inferrail pricing update`
(explicit, never silent) and `inferrail doctor` (port, catalog
freshness, provider reachability — one-line fixes).

**This is not the "control plane" `docs/adr/0004-data-plane-control-plane-boundary.md`
anticipates.** That ADR reserves the word for a *future hosted*
capability whose value comes from aggregating many requests/processes
over time or across a fleet — cross-deployment analytics, fleet
observability. What this unit builds is the opposite: a second,
localhost-only HTTP surface over *this one process's own* SQLite files,
meant to be consumed by the not-yet-built desktop app's dashboard
(v0.4.0) instead of the CLI. By ADR-0004's own test ("does this
capability's value come from a single request/process, or from
aggregating many over time/across a fleet") this is squarely data-plane
— it just happens to be a second HTTP surface instead of a CLI command.
This ADR uses "local control API" (MISSION.md's own phrase) throughout
and never "control plane" alone, specifically to avoid that confusion
for a future reader.

## Decision

**`--app-mode` is a `serve` flag, not a config file setting.** It
loads `inferrail.yaml` exactly as a normal `serve` would (providers,
routes, telemetry untouched), then forces two overrides before building
the app: `receipts.sink: sqlite` and `budgets.enabled: true`, both
relocated to fixed paths under the OS app-data directory
(`appdata.app_data_dir()` — stdlib-only, no `platformdirs` dependency,
matching this project's minimal dependency footprint: macOS `~/Library/
Application Support/inferrail`, Windows `%APPDATA%\inferrail`, Linux
`$XDG_DATA_HOME/inferrail` default `~/.local/share/inferrail`). Not
combinable with `--quickstart` — rejected with a clear error, since the
two flags answer different questions (which provider vs. where local
data lives) and quickstart's in-memory config has no `inferrail.yaml`
to load app-mode's provider/route settings from.

**Why force both overrides rather than respecting whatever
`inferrail.yaml` already says:** the local control API's receipts and
budgets routes need one canonical SQLite file each to read/write — a
partially-configured `--app-mode` (say, `receipts.sink: sqlite` but a
custom path, or `budgets.enabled: false`) would leave those routes
either broken or silently pointed somewhere the future desktop app
doesn't know to look. Forcing a fixed, predictable layout is what makes
"one flag, no `inferrail.yaml` decisions required" true for the desktop
app's own eventual "start the sidecar" step (v0.5.0).

**A mandatory, per-install bearer token — `localapi.token
.ensure_local_api_token`** — generated once (`secrets.token_urlsafe(32)`)
and persisted at `<app-data-dir>/local-api-token` with owner-only
(`0600`) permissions, printed to the console on every `--app-mode`
startup. Deliberately a new, separate mechanism from
`INFERRAIL_GATEWAY_TOKEN` (`gateway/routes.py`): that one is optional
and guards inference-cost routes; this one is mandatory (there is no
"local API disabled" mode once `--app-mode` is on) because these routes
read back a caller's own local economic history, not just proxy
inference. A new `LocalApiAuthenticationError` (`INFERRAIL_E011`) keeps
the two failure modes distinguishable in logs and error codes.

**The local API reuses domain models directly as response bodies** —
`InferenceReceipt`, `WorkSummary`, `Budget` — rather than duplicating
them into API-specific schemas. Only `ReceiptsPage` (a pagination
envelope) and `BudgetCreate` (the POST body, deliberately without a
client-supplied `budget_id` — it's computed server-side via
`budgets.schema.new_budget_id`, the same rule `inferrail budget set`
uses) are new types (`localapi/schemas.py`).

**Receipts/work/budgets endpoints share the exact same store instances
the gateway engines and `BudgetEnforcer` already hold** — `create_app`
passes the same `ReceiptsStore`/`BudgetStore` objects into
`app.state`, rather than each opening a second connection to the same
file. A budget created through `POST /v1/local/budgets` is immediately
visible to `BudgetEnforcer.check` on the next inference request, and
vice versa, because there is exactly one `BudgetStore` per process, not
two views of the same file that could drift.

**`ReceiptsStore.query()` gained `since`/`limit`/`offset`** (all
optional, defaulting to today's unfiltered/unpaginated behavior — every
existing caller is unaffected) rather than a parallel query method: the
paginated `/v1/local/receipts` endpoint uses `limit`/`offset`; the SSE
tail (`/v1/local/stream`) uses `since` to ask only for rows newer than
the last one it already sent. A new `count()` lets the paginated
endpoint report a `total` without fetching every row just to `len()` it.

**The SSE stream is a poll loop over `since`, not push-based.** SQLite
has no native pub-sub, and a fleet of one local process doesn't need
one either — polling an indexed `ts` column every second (module-level
`STREAM_POLL_INTERVAL_SECONDS`, overridable in tests) is simple, cheap,
and correct. It stops as soon as `Request.is_disconnected()` says so,
checked before every poll — same discipline `gateway/execution.py`'s
own streaming already follows.

**`inferrail pricing update` never fetches anything.** There is no
network call this command could make that would be verifiable the way
this project's other prices are (checked by hand against a vendor's own
pricing page, `docs/adr/0005`) — a live-fetched price from an
unspecified third party would be exactly the kind of unverified number
this project refuses to fabricate. Instead it reports each built-in
catalog's age (`cli/pricing.py`'s `catalog_freshness`, shared with
`inferrail doctor`) and states the one real fix: `pip install
--upgrade inferrail`, or an explicit `pricing:` override in
`inferrail.yaml`.

**`inferrail doctor`'s "provider reachability" check is a bare TCP
connect, never an HTTP request and never using a provider's own API
key** — the same question `nc -zv host port` answers, deliberately
short of anything resembling an authenticated call to a real vendor
endpoint (which could trigger vendor-side logging/rate-limit signals
for what's just a local diagnostic).

## Consequences

- A normal `inferrail serve` (no `--app-mode`) is completely unaffected
  — no new route, no new file on disk, no behavior change. Every
  existing test and config continues to work unchanged.
- The desktop app (v0.5.0) has a one-flag way to get a fully-local,
  fully-wired backend (`inferrail serve --app-mode`) without needing to
  generate or manage its own `inferrail.yaml` beyond providers/routes.
- The dashboard (v0.4.0) has a real HTTP API to build against —
  paginated receipts, work rollups, budgets CRUD, a live tail — instead
  of needing to shell out to the CLI or read SQLite files directly.
- Known scope decision: `openapi.json` (generated from the quickstart
  config, `app_mode=False`) does not include `/v1/local/*` — it
  documents the public, OpenAI/Anthropic-compatible inference surface
  only. The local API is documented in prose here and in
  `docs/PRODUCT.md`, not as a generated machine-readable spec; a future
  session could add one if the desktop app's own tooling needs it.
- `inferrail doctor`'s provider-reachability check is the first thing
  in this codebase to make an outbound network connection outside of an
  actual inference request — bounded to a bare TCP connect, and only
  ever run when a human explicitly runs `inferrail doctor`.
