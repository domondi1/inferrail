# 0017. The dashboard lives in `app/` in this repository, not a sibling repo

## Status

Accepted

## Context

`MISSION.md`'s v0.4.0 requires a React + Vite web dashboard and explicitly
flags one open architectural question: build it in a new `app/` directory
inside this repository, or in a sibling repository (`inferrail-app`)? The
v0.3.0 closing session deliberately did not guess at this — it affects repo
structure, CI, and release tooling going forward, so `PROGRESS.md` left it
for the founder to decide via ADR rather than defaulting silently.

**Founder decision: `app/` in this repository.**

## Decision

The dashboard is a TypeScript + React + Vite single-page app under `app/`
at the repository root, versioned and released alongside the Python
package rather than as an independently-versioned sibling project.

**Why same-repo over a sibling repo:**

- The dashboard has no independent release cadence — it only makes sense
  paired with a specific `inferrail` version (it talks to that version's
  local control API, `docs/adr/0016`). Splitting repos would require
  cross-repo version pinning for zero benefit at this project's current
  size (one dashboard, one backend, one team).
- `MISSION.md` explicitly says the dashboard is "served by/alongside the
  control API" — a same-repo layout keeps the API contract and its one
  consumer in the same PR/review/CI loop, the same discipline this
  project already applies to `hosted/ap_exceptions` and
  `hosted/a2a_economic_authority` (separate top-level directories, same
  repo, own CI jobs).
- A sibling repo would need its own boundary-check equivalent, its own CI,
  and its own coordination story for "which dashboard version works with
  which backend version" — real cost with no present payoff.

**How it's structured and served:**

- `app/` is a self-contained Vite project (`package.json`,
  `vite.config.ts`, `src/`) — Node/npm are dev-time-only tools for this
  directory; they are not, and must never become, a dependency of the
  Python package's own build, lint, type-check, or test steps. `ci.yml`'s
  existing jobs are untouched by this decision; a dashboard-specific CI
  job is added separately (see Consequences).
- The app builds to `app/dist/` (a static SPA — no server-side rendering,
  no Node runtime needed to serve it). `inferrail serve --app-mode`
  mounts that directory at `/dashboard` via Starlette's `StaticFiles`
  when a built `dist/` is found (`gateway/app.py`'s
  `_find_dashboard_dist`, checked in order: an `INFERRAIL_DASHBOARD_DIR`
  override, a `dashboard_static/` directory bundled inside the installed
  package — reserved for a future packaging unit, not yet populated — or
  an `app/dist` found by walking up from the source tree, which is what
  makes this work today from a git checkout). If none is found,
  `--app-mode` still starts normally (every existing guarantee is
  unaffected) and prints a one-line instruction instead of mounting
  nothing silently.
- **Client-side routing is hash-based** (`/dashboard/#/live`,
  `/dashboard/#/work`, ...), not path-based, specifically so the server
  never needs a SPA catch-all route: `StaticFiles(html=True)` only ever
  has to resolve `/dashboard`, `/dashboard/index.html`, and
  `/dashboard/assets/*`, forever, regardless of how many screens the
  dashboard grows. This is a permanent decision, not a placeholder.
- **Auth: the per-install local API token travels in the dashboard URL's
  query string**, printed by `inferrail serve --app-mode` as
  `http://<host>:<port>/dashboard/?token=<token>` (alongside the existing
  bare-token line, for anyone who wants it for `curl`). The frontend
  reads `token` from `location.search` at load time and holds it in
  memory only (never `localStorage`, so it doesn't outlive the tab).
  This is the same pattern Jupyter's classic notebook server uses for the
  same reason: it makes `MISSION.md`'s "zero terminal use after startup"
  acceptance bar real — a person opens the printed URL and the dashboard
  is already authenticated, with no copy-paste-into-a-settings-field step
  required before the Live Feed screen can show anything.
- **`localapi/routes.py`'s auth dependency now also accepts `?token=`
  as an alternative to the `Authorization: Bearer` header**, checked with
  the same `secrets.compare_digest`. This is narrowly motivated: browser
  `EventSource` (used for `GET /v1/local/stream`) cannot set custom
  request headers at all, so a header-only scheme would make the Live
  Feed screen impossible to build against the existing endpoint. Rather
  than add a second, stream-only auth path, every local API route accepts
  either form uniformly, since they already share one dependency and a
  future dashboard fetch() call gains a simpler code path for free. This
  is a deliberate, scoped exception for this one local, single-user,
  127.0.0.1-bound surface — not a pattern to copy onto the gateway's own
  (optional) `INFERRAIL_GATEWAY_TOKEN` or any hosted service's auth.

**This is unit 1 of v0.4.0, not the whole milestone.** Per the session
protocol (`MISSION.md` step 2: "pick the smallest next unit"), this pass
scaffolds the app, wires real serving/auth, and ships one working screen
(Live Feed, reading the real SSE tail). The remaining screens (Work,
Budgets, Recover, Connect, Settings) are separate, later units against
the same scaffold — see `PROGRESS.md` for exactly what's done vs. not yet.

## Consequences

- A normal `inferrail serve` (no `--app-mode`) is completely unaffected —
  no new route, no new mount, no new dependency. `create_app`'s existing
  callers (including every existing test) see no behavior change.
- `--app-mode` with no built dashboard present (e.g. a fresh clone that
  hasn't run `npm install && npm run build` in `app/`, or a `pip install`
  of a version where the packaging unit below hasn't landed yet) is not
  an error — it prints where to build the dashboard and continues; every
  other `--app-mode` guarantee (local API, SQLite receipts/budgets) is
  unaffected.
- **Known gap, explicitly deferred, not hidden:** the built dashboard is
  not yet bundled into the PyPI wheel — `pip install inferrail` today
  gets no dashboard unless the user also has this repo checked out with a
  built `app/dist` reachable from the installed package's location (which
  it normally would not be). Making `pip install inferrail` alone produce
  a working dashboard needs a packaging unit (a build hook that runs
  `npm run build` and copies `app/dist` into
  `src/inferrail/dashboard_static/` before the wheel is built, plus a CI
  job that verifies this, plus Node added to the release pipeline's
  prerequisites). Tracked as a near-term follow-up unit in `PROGRESS.md`,
  not silently assumed to already work.
- The local API token now appears in a URL (browser history, and
  Referer headers to any third-party resource the dashboard page might
  ever load — it currently loads none). Accepted for a tool bound to
  `127.0.0.1` for a single local user, matching the threat model
  `docs/adr/0016` already established for this token; flagged here so a
  future reader doesn't mistake this for an oversight.
- A dashboard-specific CI job is needed (Node setup, `npm ci`,
  `npm run build`, and eventually a frontend test runner) — added
  alongside this unit's PR, kept separate from the Python `test` job the
  same way `ap-exceptions` and `hosted-economic-authority` are already
  separate dedicated jobs.
