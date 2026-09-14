# MISSION.md — Inferrail product-engineer mandate

This file is the durable brief for whichever agent session is working
this repository. It does not change often; when it does, that's a
deliberate act, not drift. **PROGRESS.md** is the fast-moving companion —
read that one for current state.

You are Claude Code, acting as the product engineer for Inferrail
(github.com/domondi1/inferrail, site: tryinferrail.com, current released
version 0.2.0 on PyPI). You will work across many sessions — you are not
expected to finish in one. Your job in every session is to move the
project measurably closer to the two end-states below, leave the repo
releasable, and record exactly where you stopped.

## THE MISSION — two end-states that define success

**End-state 1 — a real product.** Inferrail is a complete, working
product that people genuinely care about because it improves their
working life: a local AI cost meter and workflow manager. A person
downloads Inferrail, opens it, points the AI tools they already use
(Claude Code, Cursor, the OpenAI/Anthropic SDKs, LangChain, curl) at it,
and immediately sees what every unit of AI work costs — then sets
budgets that actually block overspend at the proxy, and applies recovery
policies (retry-once-or-escalate, the existing AP engine generalized)
when a unit of work fails. All of it local, payload-free, and honest: an
unknown cost is shown as unknown, never $0.

**End-state 2 — a self-serve hosted demonstration.** The authenticated
hosted instance already exists and works, but today a visitor without a
key can only reach `GET /health`. That is the gap you must close,
precisely this way: any visitor, with no account and no human in the
loop, must be able to obtain their own short-lived sandbox credential
from the hosted service and use it to run the full hosted workflow
themselves — create a decision, record a retry attempt, record an
outcome, and read back their own report — via copy-paste commands
published on the website. Sandbox tenants are isolated, rate-limited,
auto-expiring, synthetic-data-only, and clearly labeled as such in every
response. When this works, the sentence "visitors cannot yet run that
hosted workflow themselves" is false, verifiably, from any laptop on
earth.

## HOW TO WORK — session protocol (follow this every single session)

1. **Orient.** Read MISSION.md (this file) and PROGRESS.md. If
   PROGRESS.md does not exist, create it with: current milestone,
   checklist state, decisions made, and a "next session starts here"
   pointer.
2. Pick the smallest next unit from the current milestone below. Never
   start a later milestone while an earlier one has unmet acceptance
   criteria, unless a blocker is logged in PROGRESS.md with a reason.
3. Build with the repo's existing standards: typed Python, tests at the
   same rigor already present (idempotency, crash-recovery, concurrency
   where relevant), ADRs for architectural decisions (continue the
   `docs/adr` numbering), honest-labeling language in all docs and UI
   copy.
4. Leave it releasable. Every session ends with: tests green, lint
   clean, PROGRESS.md updated, a conventional commit history, and — if a
   milestone completed — a version bump + changelog entry.
5. Flag, don't fake, manual steps. Anything requiring a human (accounts,
   secrets, DNS, payments, filming) goes into PROGRESS.md under
   `## HUMAN ACTION NEEDED` with exact instructions. Never invent
   credentials or pretend a manual step happened.
6. Never violate the non-negotiables below, even when a shortcut is
   tempting.

## NON-NEGOTIABLES (product promises — enforce them in code and copy)

- **Privacy absolute:** payload-free receipts only; prompts/responses
  never persisted; no telemetry without explicit opt-in (default off).
  Preserve ADR-0003/0005 through every new surface.
- **Honest numbers:** unknown stays unknown; overruns recorded, never
  clamped; no fabricated savings claims anywhere.
- **Enforcement is real:** a budget cap blocks or queues at the proxy,
  not a warning after the bill.
- **Works with tools people already use:** every claimed integration has
  a tested, copy-paste snippet.
- **No crypto surface in the main product:** Work Economics x402 /
  Economic Authority remain docs-only, labeled experimental, absent from
  the app, the homepage hero, and release notes headlines.
- **Deferred, not forgotten:** paid code-signing (Apple Developer
  enrollment, Windows signing account) is explicitly deferred until the
  core product is complete (milestone v0.9.x). Ship unsigned/dev-signed
  builds with clear "right-click → Open / SmartScreen" instructions
  until then. Do not let signing block any earlier milestone.

## VERIFIED STARTING STATE (do not re-litigate; build on it)

Already working: OpenAI-compatible gateway with SSE streaming + tool
calls (`inferrail serve`); payload-free receipts with JSONL sink;
`work_id` attribution, outcomes, work-level reports (work, report,
transaction); priced model catalog; AP invoice-exception recovery engine
(SQLite, idempotent, leases, crash-safe, `inferrail ap demo` verified
end-to-end); authenticated hosted AP API deployed on Render + self-host
docs; 3-OS CI; PyPI 0.2.0; Apache-2.0; MCP server; static site on GitHub
Pages.

Missing (your work): self-serve hosted sandbox; SQLite receipts store;
an Anthropic passthrough route; budget enforcement; a localhost control
API; any GUI; desktop packaging; guided onboarding; a download-centric
website.

## MILESTONES — versioned, no time estimates, each with acceptance criteria

Release each milestone when its criteria pass. Version numbers continue
from the released 0.2.0.

### v0.2.1 — "Visitors can run the hosted workflow themselves"

Closes End-state 2 first, because it is the smallest complete win. Build
in `hosted/ap_exceptions`:

- `POST /v1/sandbox` (no auth): returns `{api_key, tenant_id,
  expires_at}` — short-lived, rate-limited per IP and per key, hard caps
  on rows per tenant, auto-purged on expiry; every response from a
  sandbox tenant carries `"sandbox": true` and a synthetic-data-only
  notice field.
- Abuse guards: global sandbox-tenant ceiling, request-size limits, key
  issuance throttle, and a kill-switch env var.
- A published copy-paste walkthrough (site + `hosted/README`): four curl
  commands — get key → create decision → record retry attempt → read
  report — that a stranger can run unmodified.
- Website: replace the "visitors can only reach `/health`" section with
  the walkthrough block.

**Acceptance:** from a machine with only curl, a person with no prior
context completes the four commands successfully; a second run with an
expired key fails cleanly with a helpful error; the test suite covers
issuance, isolation, expiry, and rate limiting. Human actions to flag:
ensure the Render instance is warm/upgraded or document cold-start
honestly next to the commands.

### v0.3.0 — Core engine: measure better, and enforce

- SQLite receipts store (WAL) as a first-class sink with JSONL
  import/export; indices on `ts`/`work_id`/`project`/`model`; existing
  reports work over it.
- Anthropic `/v1/messages` passthrough with streaming + tool use, priced
  via the catalog — this makes "point Claude Code at Inferrail" true.
- Budgets with real enforcement: budget entities scoped to
  global/project/work_id with window (per-work/daily/monthly) and mode
  (warn/block); pre-flight catalog-based upper-bound estimate +
  spent-so-far check in the gateway; block responses are
  machine-readable; post-flight reconciliation records honest
  `budget_overrun_usd`. CLI: `inferrail budget set|list|rm`.
- Local control API on `127.0.0.1` with a per-install token: receipts
  query (paginated), work rollups, budgets CRUD,
  `GET /v1/local/stream` (SSE of new receipts). New
  `inferrail serve --app-mode` enables sqlite + control API + OS
  app-data dir.
- `inferrail pricing update` (explicit, never silent) and
  `inferrail doctor` (port, catalog freshness, provider reachability —
  one-line fixes).

**Acceptance:** a real request through the gateway with a $0.01 hard cap
is blocked before the provider is called and the block is visible in
the store; a Claude Code session pointed at the gateway produces
attributed receipts; crash/idempotency tests for budgets pass.

### v0.4.0 — The dashboard (web UI, no desktop shell yet)

React + Vite app in a new `app/` (or sibling repo `inferrail-app` —
decide via ADR), served by/alongside the control API, in the site's
paper-receipt design language. Screens: Live feed (receipts print in via
SSE), Work (cost per work_id/project, drill-down, unknowns rendered
honestly), Budgets (burn bars, blocked-request log), Recover (pending
human-review queue with approve/record-outcome — closing the loop the
CLI can't), Connect (per-tool snippets with copy buttons), Settings
(export, catalog refresh, opt-in ping default OFF).

**Acceptance:** `inferrail serve --app-mode` + opening the dashboard URL
lets a user watch a live request appear, set a budget, see a block, and
clear a review item — with zero terminal use after startup.

### v0.5.0 — One-click desktop app (unsigned)

- PyInstaller sidecar `inferrail-core` per OS, built in the existing
  3-OS CI, smoke-tested (demo + serve + one proxied request) against the
  built binary.
- Tauri v2 shell: supervises the sidecar (health-check, restart,
  free-port handling), menubar/tray with live "today: $X.XX", opens the
  v0.4.0 dashboard, clean shutdown, log viewer, "copy diagnostics"
  (payload-free).
- First-run onboarding: gateway started ✓ → pick your tool, copy
  snippet → send a keyless demo request → first receipt prints on
  screen.
- Auto-update wiring (Tauri updater ← GitHub Releases manifest),
  functional even while builds are unsigned.
- Release artifacts: `.dmg`, `.msi`/portable `.exe`, `.AppImage` +
  `.deb`, with `SHA256SUMS` and honest install notes for unsigned
  builds.

**Acceptance:** on a clean macOS, Windows, and Linux VM with no Python
installed, download → open → connect a tool → see a live receipt,
without a terminal (except the user pasting the snippet into their own
tool's config).

### v0.6.0 — Time-to-value and truth-in-integration

- Tested snippets + docs pages for: OpenAI SDK (py/js), Claude Code,
  Cursor, LangChain, LiteLLM, curl — each including `work_id`
  attribution.
- Un-attributed-request nudge in the dashboard with the exact header to
  add.
- `--clean` flags for all demo artifacts; every first-run failure path
  (occupied port, proxy env vars, no network, old Python for pip users)
  prints a one-line fix.
- Stopwatch script + doc for the five-minute test (download → first real
  receipt), to be run by humans.

**Acceptance:** all snippets verified against the current build in CI
where automatable; PROGRESS.md lists the human stopwatch test as
pending with instructions.

### v0.9.0 — Launch-ready: website, assets, QA, and (now) signing

- Website rebuilt around the product: OS-detecting Download buttons,
  hero screenshot of menubar + dashboard, `pip install inferrail` as the
  secondary developer path, the v0.2.1 hosted walkthrough retained,
  testnet material in footer only. README: GIF at top, download badges,
  architecture sketch.
- Repeatable release pipeline: tag → build matrix → artifacts → site
  manifest.
- QA script/doc: fresh-VM installs on all OSes, offline behavior,
  occupied port, huge streaming response, provider 500s,
  upgrade-in-place from the previous build. Security pass: control-token
  leakage, file permissions, pip-audit/cargo audit/npm audit.
- Now un-defer signing: emit precise HUMAN ACTION NEEDED instructions
  for Apple Developer enrollment + notarization secrets and the Windows
  signing decision; wire CI so that adding those secrets flips builds to
  signed with no other change.

**Acceptance:** the QA script passes end-to-end; a stranger can go from
the website to a working install and a first receipt following
on-screen instructions only.

### v1.0.0 — The complete product

Cut when: End-state 1 and End-state 2 are both demonstrably true; signed
builds are live (or the human has explicitly accepted unsigned for
launch and the site says so plainly); three people who have never seen
Inferrail hit the five-minute bar; and the demo video + screenshots
exist (human-filmed — provide them a shot list in PROGRESS.md). Ship
v1.0.0, then stop and wait for direction before any post-1.0 work
(hosted team sync, more providers, more recovery policy packs).

## STANDING HUMAN-ACTION LEDGER (maintain in PROGRESS.md, do not block on it)

Render warm/upgrade decision · signing accounts and secrets (deferred to
v0.9.0) · stopwatch tests with real people · demo video + screenshots ·
HN post authorship and timing · any paid account of any kind.

## FINAL INSTRUCTION

At the start of every session, state in one sentence which milestone and
which unit you are advancing, do the work, and end by updating
PROGRESS.md so the next session — which may be you with no memory of
this one — can continue without asking the human anything already
answered here.
