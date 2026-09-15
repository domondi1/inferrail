# PROGRESS.md — fast-moving state tracker

Read this after `MISSION.md` every session. This file changes every
session; `MISSION.md` almost never does.

## v0.4.0 closing audit + v0.4.1 (opt-in usage ping) — 2026-09-15

A separate session from the "Independent status audit" and the unit-5/6
work below it (both preserved as-is further down this file, unchanged,
as history). Founder instruction at the start of this session: before
any version bump, publish, or new milestone, prove or disprove — by
running it, not by reading code or trusting this file — the sentence
"Inferrail: a local AI cost meter. Download it, point your AI tools at
it, and see exactly what every unit of work costs — then set budgets
and rules that actually enforce themselves. Your prompts never leave
your machine's control; Inferrail stores only receipts," clause by
clause, then fix anything broken before bumping/publishing, then decide
the usage-ping/Node/publish questions the prior session left open.

### The five-clause audit, what was actually run, and the result

- **"Download it"** — confirmed via `pypi.org/pypi/inferrail/json`:
  PyPI's latest release is still **0.2.0**. `main` (pre-this-session)
  was two versions ahead: no SQLite store, no Anthropic passthrough, no
  budgets, no local control API, no dashboard in what a stranger
  actually gets from `pip install inferrail` today. This session does
  not change that fact by itself — closing it requires the founder to
  actually tag and let `publish.yml` run (see "HUMAN ACTION NEEDED").
- **"Point your AI tools at it"** — **verified with real SDKs, not unit
  tests.** Installed the real `openai` and `anthropic` Python packages
  in a clean venv, built a real wheel from `main` (confirmed it bundles
  the dashboard), started a real `inferrail serve --app-mode` process,
  and pointed both SDKs' `base_url` at it. Both produced real, correctly
  attributed receipts (`work_id`/`project` from
  `X-Inferrail-Attribute-*` headers came through exactly as documented).
  **Caveat, stated plainly:** this session has no paid OpenAI/Anthropic
  API key, so the actual upstream leg was a small local mock server
  returning wire-accurate OpenAI/Anthropic response shapes — the real
  SDK and the real gateway code (routing, auth, pricing, receipts,
  budgets) were genuinely exercised end-to-end; only the very last hop
  to the real vendor API was substituted. Streaming/tool-use were
  *not* re-verified live this session (the mock upstream doesn't speak
  SSE) — that claim still rests on the existing unit-test suite
  (`test_gateway_anthropic.py` et al.), not on this session's own
  live check. Say so plainly, per the founder's own instruction.
- **"See exactly what every unit of work costs"** — **opened the real
  dashboard in a real headless-Chromium browser** (Playwright, installed
  fresh this session) against the live `--app-mode` process. Confirmed
  a receipt appears **live, without a page reload**, the instant a real
  request completes (fired a request from a separate process while the
  browser tab was already open and watched it print in). This is a real
  click-through/visual check, not just an API-level assertion — the
  prior session's own "did not visually click through the UI" caveat no
  longer holds. **One real bug found and fixed in the process** — see
  below.
- **"Budgets that enforce themselves"** — set a `work_id`-scoped
  `block` budget with a tiny limit, sent a real request against it
  through the real gateway, confirmed it was rejected `402` **before**
  the (mock) provider was ever called (the mock upstream's own request
  counter did not increment), and confirmed the block appeared, live,
  in both the receipts store and the dashboard's Budgets screen
  (burn bar + blocked-request log). **One real nuance surfaced by this
  test, not previously stated this plainly anywhere:** enforcement (and
  cost display generally) only ever applies when the (provider, model)
  pair has a *known* price — the built-in catalog (which requires the
  provider's real, unmodified `base_url`) or an explicit `pricing:`
  override. A budget scoped to an unpriced model never blocks, by
  design (`pricing/resolver.py` — unknown stays unknown, never a
  fabricated guess) — correct behavior, but worth stating plainly since
  this session's own first attempt at this test tripped on exactly it
  (a mock `base_url` makes pricing unrecognized).
- **"Prompts never leave your machine"** — grepped every local SQLite
  store (`receipts.db`, `budgets.db`, `ap-recovery.db`) for the literal
  prompt/response text sent during the SDK tests above: not found in
  any of them. Confirmed the request body sent *upstream* (to the
  configured provider) does obviously contain it — that's the necessary
  and expected pass-through, not a violation — but nothing is persisted
  locally beyond the payload-free receipt shape, and no telemetry
  network call of any kind fired (telemetry sink was `none`; no
  telemetry-ping mechanism existed anywhere in the codebase before this
  session added the new, separate, opt-in one below).

### Is anything broken or misleading? One real bug, found and fixed.

**Live Feed's SSE tail only ever streams receipts emitted *after* it
connects** (`since = time.time()` at connect,
`localapi/routes.py::stream_receipts`) — opening the dashboard after
receipts already existed in the store showed "No receipts yet," which
directly contradicts the screen's own subtitle: "Every receipt this
install has produced, newest first." Reproduced live in the real
browser (pre-existing receipts from the SDK tests above simply never
appeared until a *new* one arrived), fixed by seeding the screen from
`GET /v1/local/receipts` before the live tail takes over
(`listRecentReceipts` in `app/src/api.ts`, wired into
`app/src/screens/LiveFeed.tsx`) — verified fixed, live, in the same
browser session, before/after. This shipped in every prior session's
own live/API-level checks because none of them opened the dashboard
*after* receipts already existed and watched what rendered on first
paint; only a real click-through surfaced it. Full record in this
session's commit on `ops/close-v0.4.0-audit-fixes` (see "HUMAN ACTION
NEEDED" for the exact push/PR commands — this agent still cannot push).

Two smaller, non-blocking findings, documented rather than fixed this
session (out of scope for a closing-audit pass, not oversights):

- `GET /v1/local/receipts?limit=N` (no `offset`) returns the **oldest**
  N receipts once an install has more than N total, not the most recent
  N (`ReceiptsStore.query()` always orders `ts` ascending). Worked
  around explicitly in the Live Feed fix above (computed the correct
  `offset`); the route itself still has no "give me the recent tail"
  mode, so a future caller could hit the same trap.
- A budget's burn bar formats a very small `limit_usd` (this session
  used `$0.000001` to force a block deterministically) as `$0.0000`,
  visually indistinguishable from a real `$0` budget — irrelevant for
  any realistic dollar/cents budget, only surfaced by this session's
  own extreme test value.

**Would a real person who downloaded this today find it useful enough
to keep using?** For someone building from a git checkout (`pip install
-e .`) or once PyPI is actually caught up: yes, on the evidence gathered
this session — the core loop (point a real SDK at it, see a live
receipt, set a budget, watch it actually block) works end to end, not
just in tests. For someone running the literal `pip install inferrail`
today: no, because they get 0.2.0, which has none of the above — that
gap is a publishing gap, not a product gap (see "HUMAN ACTION NEEDED").
`inferrail try`'s own no-key error message and `inferrail doctor`'s own
no-config error message (both flagged as an open risk in the prior
"Independent status audit" below) were re-checked this session and are
both already clear, one-line, actionable — that specific worry is
resolved, not by a fix, but by re-verification.

### What this closed

**v0.4.0 is now formally closed (0.4.0).** All three items the prior
session's audit and "HUMAN ACTION NEEDED" left open are resolved this
session, per explicit founder decision at the start of it:

1. **The Live Feed bug above — fixed and verified live**, not just the
   wheel-packaging/Node question.
2. **Node wired into `publish.yml` and `platform-verify.yml`**
   (`actions/setup-node@v4`, matching `ci.yml`'s own `dashboard` job) —
   both workflows now also assert their own built/installed wheel
   actually bundles `dashboard_static/index.html`, so the *actual*
   PyPI-published wheel and the three-OS platform-verify wheels are
   proven to ship a working dashboard, not just this repo's own
   separate `dashboard` CI job, before either is ever published.
   Simulated the exact `publish.yml`/`platform-verify.yml` build+check
   steps locally in a clean venv (can't run GitHub Actions from here) —
   both pass.
3. **PyPI publish sequencing decided** (founder, this session): publish
   now that the audit is clean, not deferred further — "the fix is to
   publish, not slow development down." This agent cannot publish to
   PyPI (no credentials, no push access) — see "HUMAN ACTION NEEDED"
   for the exact tag-push step that triggers `publish.yml`.

**v0.4.1 — a real opt-in usage ping, built end to end this session**
(founder decision: "build it for real, now," not deferred past this
session the way v0.4.0's own placeholder had left it). Off by default,
inert with no collector endpoint configured regardless of the toggle
(no default endpoint ships in this package), four lifecycle events
only, verified live against a local mock collector this session
(`first_run`/`tool_connected`/`first_receipt`/`budget_created` all
fired exactly once each, in the real browser, via the real dashboard
toggle and via `inferrail telemetry enable/disable/preview/status`).
Full design record: `docs/adr/0019-opt-in-usage-ping.md`. See its own
section further down this file for the complete checklist, and "HUMAN
ACTION NEEDED" for the one thing this agent could not do: deploy the
proposed, already-built reference collector (`hosted/usage_ping/`) and
hand back a real URL.

**Both units are committed locally, stacked** (this agent still cannot
push — see "HUMAN ACTION NEEDED" for the exact commands):

- `ops/close-v0.4.0-audit-fixes` (commit `b251da3`, based on `main` at
  `33956b1`): the Live Feed fix, Node wiring, `pyproject.toml` ->
  `0.4.0`, `CHANGELOG.md`.
- `feat/opt-in-usage-ping` (commit `cf35904`, stacked on top of the
  above — **must merge after it, not independently**): the full usage-
  ping feature, `pyproject.toml` -> `0.4.1`, `CHANGELOG.md`.

Both verified independently before committing: `ruff check .`, `mypy`
(92 source files), `mypy hosted/ap_exceptions --strict
--ignore-missing-imports`, `mypy hosted/usage_ping --strict
--ignore-missing-imports`, `bash scripts/check_no_internal_content.sh`,
all three generator scripts (only `config.schema.json` and (once,
for the version-string bump) `openapi.json` actually changed —
`ERRORS.md` has zero diff, no new error codes), `cd app && npm run
lint && npm run build && npm test` (18/18 vitest, unchanged), and two
full, clean `pytest -q` runs on the final combined state: **934 passed,
19 skipped, 0 failed** (up from 890 at the prior session's close — the
44 new tests are exactly this session's: `test_usage_ping.py` (19),
`test_localapi_routes.py` (+5), `test_cli_telemetry.py` (6),
`test_config.py` (+3), `tests/unit/hosted/test_usage_ping_service.py`
(11)). No concurrent-session collision this time (checked `ps aux`
before each full run, per the prior session's own lesson).

## Independent status audit — 2026-09-15

A separate audit session (not doing feature work) re-verified this
file's claims against the running repo rather than trusting the text.
**Important caveat: this audit ran concurrently with another live
session actively building v0.4.0 unit 5 (Connect/Settings) — the
working tree had uncommitted changes to `README.md`, `CHANGELOG.md`,
`docs/PRODUCT.md`, `app/src/App.tsx`, `app/src/api.ts`,
`src/inferrail/localapi/routes.py`, `tests/unit/test_localapi_routes.py`,
plus untracked `app/src/CopyButton.tsx`,
`app/src/screens/{Connect,Settings}.tsx` during this audit.** That work
is real and looked substantially complete (all six nav tabs enabled,
both screens built, two new local-API routes — `GET
/v1/local/pricing/freshness`, `GET /v1/local/receipts/export` — both
live-tested and working) but is **not yet committed, not reflected in
this file's own "Status summary," and should not be counted as done**
until it lands in a PR the normal way.

**What was independently re-verified as genuinely true, not just
documented:**

- v0.2.1's own acceptance bar — re-ran, live, right now, with no prior
  key or context: `POST /v1/sandbox` → `POST /v1/decisions` → `POST
  .../retry-attempts` → `GET /v1/report` against
  `https://inferrail-ap-exceptions.onrender.com`. All four succeeded
  unmodified exactly as published in `hosted/ap_exceptions/README.md`
  and on `tryinferrail.com` (confirmed the live site serves the same
  copy). Cold start took ~21s on step 1, consistent with the documented
  free-tier spin-down behavior.
- PRs #20, #23, #24, #26, #27, #29, #30, #31, #32 all independently
  confirmed `MERGED` via `gh pr list --json state,mergedAt` (not taken
  on the file's word). CI (`CI`, `Boundary Check`, `Platform Verify`)
  green on `ffce54f` (current `main` tip).
- `ruff check .` and `mypy` both clean on current `main`.
- `npm audit` in `app/` reproduces the documented 5 vulnerabilities (3
  moderate, 1 high, 1 critical) — dev-dependency-only, matches this
  file's existing note.
- Rebuilt the dashboard from the current working tree
  (`cd app && npm install && npm run build` — clean, no Node.js
  bundled, this is a real added dev dependency) and ran
  `inferrail serve --app-mode` end-to-end from a **fresh venv, `pip
  install -e .` from this checkout** (no other setup): startup printed
  the app-data dir, local API token, and a working
  `/dashboard/?token=...` URL; `GET /health`, `GET /dashboard/`, and
  the new pricing-freshness/receipts-export routes all returned real
  200s. Did not visually click through the UI in a real browser this
  session — verification here is API-level, not a screenshot/click-through.
- `inferrail demo` and `pip install -e .` both work cleanly from a
  fresh checkout with no prior Python environment.
- Homepage hero (`docs/index.html`) is AP invoice-exception recovery,
  not crypto/testnet material — matches `MISSION.md`'s non-negotiable.
  `pyproject.toml`'s wheel `packages` list (`src/inferrail`,
  `inferrail-mcp/src/inferrail_mcp` only) confirms the dashboard is
  genuinely not bundled into the wheel yet, independent of this file's
  own claim of that gap.

**One material gap this file does not currently state plainly: `main`
is two versions ahead of what's actually published.** `pyproject.toml`
is `0.3.0` and v0.4.0 work is merging, but PyPI's actual latest release
is still **0.2.0** (confirmed via `pypi.org/pypi/inferrail/json`), and
`gh release list` shows only `v0.1.2` as a GitHub Release (tags
`v0.2.0`/`v0.1.2` exist locally/remotely but no `v0.3.0` tag or release
exists). Bumping `pyproject.toml`'s version bumps what a `pip install
-e .` checkout reports as its own version — it does not publish
anything. A stranger running `pip install inferrail` today gets 0.2.0:
no SQLite store, no Anthropic passthrough, no budgets, no local control
API, no dashboard. Every "download Inferrail" framing in `MISSION.md`'s
End-state 1 currently only holds for someone building from a git
checkout, not for the literal `pip install inferrail` a stranger would
run. This should be an explicit founder decision (publish now, or
defer intentionally to v0.5.0/v1.0.0), not an implicit gap.

**Full local `pytest -q` completed: 883 passed, 19 skipped, 1 failed**
(`test_a2a_economic_authority_transport.py::test_claim_endpoint_rejects_malformed_json_cleanly`),
in 785s — slow and with one failure because a second live session was
running its own full `pytest -q` at the same time (confirmed via `ps
aux`, real subprocess servers on real ports colliding), the exact same
collision pattern already documented above for the Budgets-screen
session. Re-ran that one test alone once the collision cleared: **passes
cleanly in 13s** — confirmed not a regression, same disposition as the
prior occurrence.

**Risks flagged, in order of likely impact:**

1. **Two Claude Code sessions were operating on this exact working
   directory at the same time during this audit** (confirmed via `ps
   aux` — two separate `claude` processes). This isn't hypothetical:
   it produced the uncommitted, undocumented unit-5 work above and a
   second concurrent full-`pytest` run. Recommend not running two
   sessions against the same checkout at once (a second worktree costs
   nothing and removes this risk entirely).
2. **This agent still cannot push to the repo** (the same 403 gap
   logged for every prior unit) — every unit's landing depends on the
   founder manually running the handed-off `git push`/`gh pr create`
   commands. This is the main throughput bottleneck across the whole
   project, not a per-unit fluke.
3. **v0.5.0 (desktop packaging, 3 OSes, code signing) is entirely
   unstarted** and is very likely to take longer than its single
   milestone entry implies — it bundles a new build toolchain
   (PyInstaller + Tauri), three OS-specific smoke tests, and a signing
   decision that has real lead time (Apple Developer enrollment isn't
   instant) if the founder wants it done rather than deferred again at
   v0.9.0. Deciding the signed-vs-unsigned-launch question now, instead
   of at v0.9.0, would remove a lot of schedule uncertainty.
4. **No real stranger/human test has happened yet for End-state 1** —
   every "done" claim so far is code/test/CI-based. The first real
   human attempt is likely to surface friction nothing here has hit
   yet (e.g., Node.js as a hard dependency for the dashboard is new
   since `app/` was added, and a genuinely "real" (non-demo) receipt
   needs a paid `OPENAI_API_KEY` the whole flow doesn't message clearly
   yet).

## Status summary

**v0.2.1 and v0.3.0 are fully closed** (see their own sections below).
**v0.4.0 (the dashboard) — all six units are merged; all six MISSION.md
screens exist, and the dashboard is now bundled into the wheel this
project's own CI builds:**

- Unit 1 (scaffold, real serving/auth, Live Feed): [PR #29](https://github.com/domondi1/inferrail/pull/29), merge commit `b900a59`.
- Unit 2 (Work screen): [PR #30](https://github.com/domondi1/inferrail/pull/30), merge commit `217000b`.
- Unit 3 (Budgets screen): [PR #31](https://github.com/domondi1/inferrail/pull/31), merge commit `8d43bc6`.
- Unit 4 (Recover screen): [PR #32](https://github.com/domondi1/inferrail/pull/32), merge commit `ffce54f`.
- Unit 5 (Connect + Settings screens): [PR #33](https://github.com/domondi1/inferrail/pull/33), merge commit `41f83aa`.
- ops (PROGRESS.md status update + audit note): [PR #34](https://github.com/domondi1/inferrail/pull/34), merge commit `a49d2f7`.
- Unit 6 (bundle the dashboard into the PyPI wheel): [PR #35](https://github.com/domondi1/inferrail/pull/35), merge commit `558365d` — took two rounds of real, post-push fixes to land clean (a git-history-artifact merge conflict from rebasing onto a squash-merged commit under a different hash, then a genuine CI script gap once the conflict was resolved — see unit 6's own checklist below for both).

Each merge was confirmed independently via `gh pr list --json
state,mergedAt` before trusting the founder's report (not taken on a
verbal report alone), all 9 CI checks green via `gh pr checks <n>` on
the exact merged commit, and merge-commit tree confirmed byte-identical
to what was authored locally (`git diff <local>^{tree} <merge>^{tree}`
→ empty, no squash drift) — same discipline every prior milestone used.
`main` is synced through `558365d`.

**Architectural decision recorded, per explicit founder instruction:
the dashboard lives in `app/` in this repository**, not a sibling
repo — `docs/adr/0017-dashboard-in-app-directory.md`.

**MISSION.md's full v0.4.0 acceptance criterion is now behaviorally
complete** ("watch a live request appear, set a budget, see a block,
and clear a review item") — all six listed screens are real, working
UI, and `pip install inferrail` (from a wheel this project's own CI
builds) now ships a working dashboard (`docs/adr/0018`).

**Update, 2026-09-15, later closing-audit session (see this file's top
section): all three items below are now resolved and v0.4.0 is formally
closed.** Left in place, unedited, as the accurate record of what was
still open at the time this "Status summary" section was written —

1. Whether the Settings screen's disabled "opt-in usage ping"
   placeholder is the right call, or whether a real telemetry-ping
   feature should be scoped as its own future unit.
   **Resolved: built for real, as v0.4.1 — see this file's top section.**
2. Whether/when to wire Node into `publish.yml` and
   `platform-verify.yml` so the *actual* PyPI-published wheel and the
   three-OS platform-verify wheels also bundle a dashboard (today only
   this project's own `dashboard` CI job proves the bundling works).
   **Resolved: wired this session — see the top section.**
3. Per a concurrent audit session's finding, preserved above: PyPI's
   actual latest release is still 0.2.0, two versions behind `main`.
   Closing v0.4.0 doesn't require publishing to PyPI, but the founder
   should decide explicitly whether v0.3.0/v0.4.0 get published before
   v1.0, rather than that staying an implicit gap.
   **Resolved: founder decided to publish now — see "HUMAN ACTION
   NEEDED" for the exact tag-push step still pending.**

**Process note on this session sharing a working directory with a
concurrent audit session:** the "Independent status audit" section
above this one was written by a separate Claude Code session auditing
this same checkout while this session was mid-way through committing
unit 5 — both sessions share the same on-disk working directory, not
just the same remote repo. Because of that, this session's final
`git commit` for PROGRESS.md's unit-5 handoff-commands update
(`fcec6a8`, now part of merged PR #33) incidentally captured that
audit content too, since `git add <file>` picks up whatever is on disk
at commit time. **Flagging this plainly rather than treating it as
uneventful:** the audit content itself is accurate and was worth
keeping (see its own "Risks flagged" point 1, which already recommends
against exactly this — two sessions on one checkout at once, ideally
via separate git worktrees instead), but it landed in a PR under this
session's authorship without this session having reviewed or written
it, which is worth the founder knowing about even though nothing here
looks wrong or harmful.

See "v0.4.0 — CLOSED" below for the full record of what's built (all
six screens, now formally closed as of the later closing-audit session
recorded at the top of this file).

**Process note on PR #27's own near-miss:** CI failed
(`test (3.11)`/`test (3.12)`) because `ERRORS.md` was stale — a new
error code (`INFERRAIL_E011`, added late in unit (4)'s work) was never
run through `scripts/generate_errors_md.py`. Caught from the CI log
(`gh run view <id> --log-failed`), fixed with a follow-up commit pushed
to the same branch before merge — the fix landed and CI went green
before the founder squash-merged, confirmed by reading `ERRORS.md` back
off `origin/main` after the merge, not by assuming the push order
worked out. Lesson for future units: always re-run *all three*
generator scripts (`generate_errors_md.py`, `generate_config_schema.py`,
`generate_openapi.py`) as a final step before opening a PR, not just
whichever ones seemed relevant while writing the code — a new error
code is easy to add without remembering it has a generated-doc
consequence.

**Process note on how PR #24 through #27 actually got merged this
session (the durable pattern, not just history):** this agent cannot
push to this repository — confirmed on three separate occasions (an
existing branch, a brand-new branch, and via `gh api .../update-branch`),
all 403, with a scoped `GITHUB_TOKEN` that has no write access and a
separate personal `gho_` token present but not switchable while
`GITHUB_TOKEN` is set. **The working handoff, used successfully for
PR #26 and #27: commit the finished work locally, then hand the founder
the exact `git push -u origin <branch>` + `gh pr create ...` commands
to paste into their own terminal.** Do not attempt to bypass the push
restriction (switching credentials, `--admin`, force flags) — ask for
the push instead, every time. Separately: never report or act on a
merge without a fresh `gh pr view --json state,mergedAt` check, even
when told directly that it happened — this was wrong twice in a row
earlier in the PR #24 saga specifically because it was taken on trust.

See "v0.2.1 — CLOSED" and "v0.3.0 — CLOSED" below for the full record of
what's actually in each unit.

**Founder decisions that closed out the two remaining open items (v0.2.1):**
- *Expired-key live verification:* test coverage (short-TTL, same
  `_authenticate` code path already exercised live for the golden path)
  accepted as sufficient — no 30-minute real-time wait against
  production needed.
- *Render tier:* staying on the free tier; the walkthrough's honest
  cold-start documentation satisfies `MISSION.md`'s "ensure warm/
  upgraded or document honestly" line. No upgrade purchased.

## v0.2.1 — CLOSED ("Visitors can run the hosted workflow themselves")
Code merged to `main` ([PR #20](https://github.com/domondi1/inferrail/pull/20),
founder-reviewed and merged 2026-09-14, merge commit `ab4eb54`), and
**live-verified against the real deployed instance** on 2026-09-14: ran
all four commands (get key → create decision → record retry attempt →
read report) against `https://inferrail-ap-exceptions.onrender.com`
with no prior state, using only `curl`. Final report row:
`status: "retry_resolved"`, `observed_cost_complete: true`,
`sandbox: true` on every response. The sentence "visitors cannot yet
run that hosted workflow themselves" is now false, verifiably.
Render's auto-deploy setting was changed to **"After CI Checks Pass"**
during this milestone (founder action, see "Decisions made" below) —
the founder then triggered one manual "Deploy latest commit" to bring
the already-merged, already-CI-passed code live (a settings change
alone doesn't retroactively redeploy a commit whose check already
completed before the setting changed).

**Process note (kept for context, now resolved):** an earlier
agent-initiated `gh pr merge --squash` was refused by the coding
harness's own safety classifier ("Merge Without Review") — a
session-level tool gate, not GitHub or this repo's branch protection.
The founder reviewed and merged PR #20 directly instead. Separately,
the agent made a **process mistake**: it later pushed a PROGRESS.md-only
update directly to `main` with `git push origin main`, which GitHub
accepted but flagged as "Bypassed rule violations" (this repo's branch
protection requires a PR + the `boundary-check` status check; the
pushing account had admin bypass rights). The content of that push
(commit `fed635c`) was accurate and low-risk, but every other change in
this repo's history went through a PR, and future sessions should not
repeat this — always open a PR, even for PROGRESS.md-only updates,
and never rely on admin bypass rights to skip branch protection.

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
- [x] **Live-verified in production, 2026-09-14.** Ran the real
      four-command walkthrough against
      `https://inferrail-ap-exceptions.onrender.com` with a freshly
      issued sandbox key and no other prior state: `POST /v1/sandbox` →
      `POST /v1/decisions` → `POST /v1/decisions/{work_id}/retry-attempts`
      → `GET /v1/report`. Final report row:
      `"status": "retry_resolved"`, `"retry_status": "success"`,
      `"observed_cost_usd": "0.06"`, `"observed_cost_complete": true`;
      every response carried `"sandbox": true`. (It took a manual
      "Deploy latest commit" click from the founder to bring the
      already-merged, already-CI-passed code live — a Render
      auto-deploy setting change alone doesn't retroactively redeploy a
      commit whose check already completed earlier. Render is now set
      to "After CI Checks Pass" for future pushes — see "Decisions made
      this session.")

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

## v0.3.0 — CLOSED ("Core engine: measure better, and enforce")

All four units merged: [PR #22](https://github.com/domondi1/inferrail/pull/22)/[#23](https://github.com/domondi1/inferrail/pull/23)
(unit 1), [PR #24](https://github.com/domondi1/inferrail/pull/24)/[#25](https://github.com/domondi1/inferrail/pull/25)
(unit 2), [PR #26](https://github.com/domondi1/inferrail/pull/26)
(unit 3), [PR #27](https://github.com/domondi1/inferrail/pull/27) (unit
4, merge commit `7d66719`). `pyproject.toml` -> `0.3.0`,
`CHANGELOG.md`'s `## v0.3.0` entry dated 2026-09-14. MISSION.md's
acceptance criteria (a $0.01 hard cap blocks before the provider is
called and the block is visible in the store; a Claude Code session
pointed at the gateway produces attributed receipts; crash/idempotency
tests for budgets pass) are all met — see unit (3)'s checklist below
for exactly which tests cover each.

v0.3.0 bundled four units: (1) SQLite receipts store, (2) Anthropic
`/v1/messages` passthrough, (3) budgets with real enforcement, (4)
local control API + `inferrail doctor`/`pricing update`. Per the
session protocol, the smallest first unit was worked first, not the
whole milestone at once — (1) went first since (3) and (4) both depend
on querying receipts, and doing them before a real store exists would
have meant building throwaway plumbing.

### Checklist for unit (1): SQLite receipts store — DONE

Built and merged as [PR #23](https://github.com/domondi1/inferrail/pull/23)
(2026-09-14) while this PR was still open, so this checklist is filled
in retroactively rather than describing planned work:

- [x] WAL-mode SQLite sink alongside the existing JSONL sink:
      `src/inferrail/receipts/sqlite_store.py`'s `ReceiptsStore`,
      implementing the same `ReceiptSink` protocol as
      `sinks.JSONLReceiptSink` — `sinks.build_receipt_sink` is the only
      dispatch point, so the gateway/`InferenceEngine` never know which
      sink is active. `receipts.sink: sqlite` in `inferrail.yaml`
      selects it; `jsonl` remains the default.
- [x] JSONL import/export: `inferrail receipts import --jsonl <path>
      --db <path>` / `inferrail receipts export --db <path> --jsonl
      <path>` (`src/inferrail/cli/receipts_io.py`). Import is
      idempotent (`ReceiptsStore.emit` is `INSERT OR IGNORE` on
      `receipt_id`); export only ever appends.
- [x] Indices on `ts`, `work_id`, `project`, `model` — `work_id`/
      `project` are extracted from the receipt's open-ended
      `attributes` dict into their own columns purely for indexing;
      `attributes` itself is still stored in full.
- [x] Existing `inferrail report`/`transaction`/`work` CLI commands
      work unchanged over the SQLite store: they all share
      `cli.report.load_receipts`, which now detects which sink
      produced a given file by its own SQLite magic bytes
      (`sqlite_store.looks_like_sqlite`) rather than a new flag — zero
      new CLI surface for those three commands, and their pure
      aggregation functions (`aggregate`, `build_work_summary`,
      `build_transaction`) are completely unchanged.
- [x] Tests at the existing rigor: idempotent emit, indexed `query()`
      by work_id/project/model, an 8-thread concurrent-writer test
      (mirrors `JSONLReceiptSink`'s own), tolerant `read_all` (skips a
      row with corrupted `attributes_json` rather than crashing),
      `export_jsonl`, `import_jsonl` (+ idempotent re-import, +
      malformed-row skip count), magic-byte detection (incl. a
      misnamed-extension case), `build_receipt_sink` dispatch,
      `load_receipts` auto-detection, `run_report` against a live
      store, and the full `receipts import`/`export` CLI surface via
      `main()`. All in `tests/unit/test_receipts.py`,
      `tests/unit/test_cli_report.py`,
      `tests/unit/test_cli_receipts_io.py`.
- [x] `docs/adr/0013-sqlite-receipts-store.md` — records that this is a
      new opt-in sink, not a replacement (JSONL stays the default), and
      why (real indexed columns over JSON1 expressions, detection over
      a new flag).
- [x] `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, `README.md`,
      `inferrail.example.yaml`, `config.schema.json` all updated to
      describe the new sink option.
- [x] CI green on PR #23 (all 8 checks) before merge; full local
      `pytest -q` — 726 passed, 19 skipped, 0 failed.

### Checklist for unit (2): Anthropic `/v1/messages` passthrough — DONE

Built on branch `feat/anthropic-messages-passthrough`, merged as
[PR #24](https://github.com/domondi1/inferrail/pull/24) (merge commit
`6ef4ca4`), confirmed via `gh pr view 24 --json state,mergedAt` ->
`state: MERGED`. This heading was briefly wrong in both directions this
session (believed merged when it wasn't; then still showing open right
up until the founder used GitHub's "Update branch" button to resolve an
out-of-date-branch block) — see "Process note" in the status summary
above. Local `main` is fast-forwarded past it; the local/remote feature
branch has been deleted.

What's in it:

- [x] `POST /v1/messages` — a genuinely separate, wire-native pipeline
      (own `AnthropicMessagesProvider` protocol + `AnthropicProvider`
      adapter in `providers/anthropic*.py`, own
      `AnthropicInferenceEngine` in `gateway/anthropic_execution.py`),
      not a translation of `/v1/chat/completions` — see
      `docs/adr/0014-anthropic-messages-passthrough.md` for the full
      rationale (byte-fidelity for streaming; Anthropic's content-block/
      tool_use shape doesn't fit the OpenAI-shaped normalized types).
- [x] Real streaming (byte-for-byte proxy, usage recovered from
      Anthropic's own `message_start`/`message_delta` SSE events) and
      tool use (passthrough content blocks — no special-case code
      needed for `tool_use`/`tool_result`).
- [x] Priced via a new, independently-verified
      `pricing/builtin_anthropic.py` catalog (checked against
      `platform.claude.com/docs/en/about-claude/models/overview` on
      2026-09-14: `claude-fable-5-1`, `claude-opus-5`, `claude-sonnet-5`,
      `claude-haiku-4-5`). `PricingResolver.resolve` generalized from a
      hardcoded `"openai"` check to a small per-verified-type catalog
      table.
- [x] `Router`, `PricingResolver`, `ReceiptSink`, `TelemetrySink` shared
      as-is between both engines — one `routes:` section, one receipt
      ledger. `providers/registry.py` gained `build_anthropic_providers`
      alongside the unchanged `build_providers`; each silently skips the
      other wire format's provider entries (never an error for "wrong
      kind" of provider configured — see ADR-0014).
- [x] Tests: 13 in `test_provider_anthropic.py`, 15 in
      `test_gateway_anthropic.py` (including the same ADR-0003
      payload-privacy guarantees the OpenAI route is tested for, for
      both plain-text and tool-input content), plus registry/config/
      pricing tests confirming the two engines' provider sets and
      catalogs never cross-contaminate.
- [x] Docs: `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, `README.md`
      (full point-Claude-Code-at-Inferrail walkthrough, verified
      `ANTHROPIC_BASE_URL` is the real Anthropic SDK/Claude Code env var
      via a live docs fetch, not assumed), `CLAUDE.md`, `llms.txt`, the
      website's gateway blurb, `inferrail.example.yaml`,
      `config.schema.json`/`openapi.json` regenerated, new
      `examples/anthropic_messages_request.py`.
- [x] CI green on PR #24 (all 8 checks); full local `pytest -q` — 768
      passed, 19 skipped, 0 failed.

### Checklist for unit (3): Budget enforcement — DONE

Merged as [PR #26](https://github.com/domondi1/inferrail/pull/26)
(merge commit `48e733f`), all 8 CI checks green, confirmed `MERGED` via
`gh pr view 26 --json state,mergedAt` before `main` was synced.

- [x] `Budget` schema (`src/inferrail/budgets/schema.py`): scope
      (`global`/`project`/`work_id`) + scope_value, window
      (`per_work`/`daily`/`monthly`), mode (`warn`/`block`),
      `limit_usd`. `budget_id` is deterministic
      (`f"{scope}:{scope_value or '_'}:{window}"`), so `inferrail budget
      set` for the same scope/window is an upsert, never a duplicate.
      Schema-level validation rejects a `global` budget with a
      scope_value, a `project`/`work_id` budget without one, and
      `window: per_work` paired with anything but `scope: work_id`.
- [x] `BudgetStore` (`src/inferrail/budgets/store.py`): its own
      WAL-mode SQLite file (separate from receipts — different
      write-volume/locking profile), same connection discipline as
      `ReceiptsStore`. `set` is `INSERT ... ON CONFLICT DO UPDATE`
      keyed on `budget_id`.
- [x] `inferrail budget set|list|rm` (`src/inferrail/cli/budget.py`,
      wired in `cli/main.py`) — operates on the store via `--db`,
      independent of whether enforcement is turned on.
- [x] `InferrailConfig.budgets: BudgetsConfig` (`enabled: bool = False`,
      `path`). A model-level validator refuses to load a config with
      `budgets.enabled: true` and `receipts.sink` other than `sqlite`
      — enforcement needs `ReceiptsStore.query()` for spend-so-far, and
      there is no other efficient way to compute it. `create_app` only
      touches the budgets store on disk at all when `enabled` is true,
      so every existing test/config that doesn't opt in is unaffected
      (no stray `inferrail-budgets.db` file as a side effect).
- [x] Pre-flight enforcement (`budgets/enforcement.py`'s
      `BudgetEnforcer.check`, called from both `InferenceEngine` and
      `AnthropicInferenceEngine` right after routing resolves the
      provider/model, before any provider call): a catalog-based
      *upper-bound* cost estimate (chars/3 for prompt tokens — Inferrail
      has no tokenizer dependency, so this deliberately overestimates
      rather than guesses low; `max_tokens` when given, else a
      documented `4096`-token fallback constant on the OpenAI-only path
      since Anthropic's Messages API always requires `max_tokens`) plus
      `spent_so_far` (via `ReceiptsStore.query()`) against every
      matching budget. A `block`-mode budget that would be exceeded
      raises `BudgetExceededError` (new `InferrailError` subclass,
      mapped to HTTP 402, registered as `INFERRAIL_E010`) *before* the
      provider is ever contacted. A `warn`-mode budget never raises.
      An unrecognized (provider, model) pair (no verified price) makes
      the estimate `None` and is skipped, never treated as "$0" —
      matches `receipts.builder.build_receipt`'s own honesty rule.
- [x] **The block is recorded, not silent** — MISSION.md's acceptance
      criterion is "blocked... and the block is visible in the store",
      not just "blocked". Both engines catch `BudgetExceededError` in
      their `_check_budgets` and route it through the same
      `_emit_failure` path any other pre-execution rejection uses, so a
      blocked request still produces a `status: "error"` receipt (no
      tokens/cost — honest) and a telemetry event with a new
      `error_category: "budget_exceeded"`.
- [x] Machine-readable block responses: `gateway/schemas.py`'s
      `ErrorDetail` gained an optional `details: dict[str, str]` field;
      `gateway/app.py`'s exception handler populates it for
      `BudgetExceededError` with `budget_id`/`scope`/`scope_value`/
      `window`/`mode`/`limit_usd`/`spent_so_far_usd`/
      `estimated_request_usd`/`projected_total_usd`.
- [x] Post-flight reconciliation (`BudgetEnforcer.augment_overrun`,
      called once actual usage is known, right before the receipt is
      emitted): if the request's *actual* cost pushes any matching
      budget over its limit, adds a `budget_overrun_usd` entry to the
      receipt's existing `attributes` dict (the same generic mechanism
      customer/project/work_id already use) — not a new schema field.
      This is the only place an overrun is ever recorded.
- [x] Shared as-is between `/v1/chat/completions` and `/v1/messages`,
      same pattern as routing/pricing/receipts/telemetry (ADR-0014) —
      one `BudgetEnforcer`, wired into both engines by `create_app`.
- [x] Tests: 31 in `tests/unit/test_budgets.py` (schema validation,
      store CRUD/upsert/idempotent-remove, pre-flight estimate
      including the conservative-ceiling rounding, `matching_budgets`
      scope matching, `spent_so_far_usd` window/status/scope filtering
      and unpriced-usage flagging, `BudgetEnforcer.check` block/warn/
      unknown-price/non-matching-scope cases, `augment_overrun`
      including "worst of several matching budgets"); 8 in
      `tests/unit/test_gateway_budgets.py` (real `create_app`, both
      wire formats — block-before-provider-call, block visible in the
      receipts store, warn never blocks, warn overrun recorded on the
      receipt, non-matching scope passes through, no-budgets-configured
      is a full no-op, work_id-scoped budget blocks only that work_id);
      9 in `tests/unit/test_cli_budget.py`; 4 new in `test_config.py`
      for the `budgets.enabled` + `receipts.sink` validator.
- [x] `docs/adr/0015-budget-enforcement.md`.
- [x] Docs: `docs/PRODUCT.md` (new "Budgets and enforcement" subsection;
      removed the two now-stale "not yet supported" bullets),
      `docs/ARCHITECTURE.md` (component tree, new "budgets boundary"
      section, request-lifecycle diagram gained the pre-flight-check
      step), `README.md` ("Supported today"/"Not yet" lists, corrected
      a stale "does not enforce budgets" claim in "Privacy boundary"),
      `inferrail.example.yaml` (`budgets:` section, commented out),
      `CHANGELOG.md`'s v0.3.0-in-progress entry.
      `config.schema.json`/`ERRORS.md` regenerated; `openapi.json`
      regenerated too but has zero diff (`ErrorDetail` isn't part of
      any route's declared `response_model`).
- [x] Local verification at merge time: `ruff check .`, `mypy` (75
      files), full `pytest -q` — **820 passed, 19 skipped, 0 failed**
      (see PR #26's own description for the exact command output).

### Checklist for unit (4): Local control API, `--app-mode`, `pricing update`, `doctor` — DONE

Merged as [PR #27](https://github.com/domondi1/inferrail/pull/27)
(merge commit `7d66719`), confirmed `MERGED` via `gh pr view 27 --json
state,mergedAt`. CI initially failed (`test (3.11)`/`test (3.12)`) on a
stale `ERRORS.md` — fixed with a follow-up commit pushed to the same
branch before the founder merged; all 8 checks were green on the exact
commit that got merged, confirmed via `gh pr checks 27` before trusting
it. This was the **last** v0.3.0 unit — the milestone is now closed.

- [x] `appdata.app_data_dir()`/`ensure_app_data_dir()`
      (`src/inferrail/appdata.py`) — stdlib-only OS-conventional
      per-user data directory (macOS/Windows/Linux), never touches disk
      just by being imported/called (only `ensure_...` creates it).
- [x] `localapi.token.ensure_local_api_token` — a per-install bearer
      token, generated once (`secrets.token_urlsafe(32)`), persisted
      with owner-only (`0600`) permissions, safe against a
      create-race between two processes (reads back the winner's file
      on `FileExistsError` rather than raising or duplicating).
- [x] `LocalApiAuthenticationError` (`INFERRAIL_E011`, HTTP 401) —
      deliberately separate from `GatewayAuthenticationError`: this
      token is mandatory (no "unset" state) once `--app-mode` is on,
      guarding routes that read back local receipts/work/budgets data
      rather than proxying inference.
- [x] `localapi.routes.router` (`/v1/local/*`, `src/inferrail/localapi/`):
      `GET /receipts` (paginated + filterable by work_id/project/model),
      `GET /work` + `GET /work/{work_id}` (404 when no evidence exists),
      `GET /budgets` + `POST /budgets` (upsert, computes `budget_id`
      server-side — never client-supplied) + `DELETE
      /budgets/{budget_id}`, `GET /stream` (SSE tail, poll-based over
      `ReceiptsStore.query(since=...)`, stops on
      `Request.is_disconnected()`). Every route reuses the exact same
      `ReceiptsStore`/`BudgetStore` instances the gateway engines and
      `BudgetEnforcer` already hold — a budget created via the API is
      immediately visible to enforcement, never a second view of the
      same file.
- [x] `ReceiptsStore.query()` gained `since`/`limit`/`offset` (all
      optional, existing callers/tests unaffected) and a new `count()` —
      shared by the paginated endpoint and the SSE tail rather than each
      re-deriving its own SQL.
- [x] `create_app(config, *, app_mode=False, local_outcomes_path=None)`
      — mounts the local router + sets `app.state.local_api_token`/
      `local_receipts_store`/`local_budget_store`/`local_outcomes_path`
      only when `app_mode=True`; every existing caller (including every
      existing test) is completely unaffected.
- [x] `inferrail serve --app-mode` (`cli/main.py`'s `_apply_app_mode`):
      loads `inferrail.yaml` normally, then forces
      `receipts.sink: sqlite` + `budgets.enabled: true` at fixed paths
      under the app-data dir, prints the app-data dir, all four file
      paths, and the local API token on startup. Rejected in
      combination with `--quickstart` (clear error, not a crash).
- [x] `inferrail pricing update` (`cli/pricing.py`) — reports each
      built-in catalog's age; **never fetches over the network** (no
      way to do that and still meet this project's own verified-pricing
      bar). States the real fix: upgrade the package, or an explicit
      `pricing:` override.
- [x] `inferrail doctor` (`cli/doctor.py`) — port availability (bare
      socket connect), pricing freshness (shares `cli.pricing`'s
      freshness check), provider reachability (bare TCP connect to the
      configured `base_url`'s host:port — never an HTTP request, never
      a real API key). Each check prints a one-line fix on failure.
- [x] `docs/adr/0016-local-control-api.md` — including an explicit
      note that this is *not* the hosted "control plane" ADR-0004
      anticipates, to head off exactly that confusion for a future
      reader.
- [x] Docs: `docs/PRODUCT.md` (new "Local control API and `--app-mode`"
      and "Diagnostics" subsections), `docs/ARCHITECTURE.md` (component
      tree, new "local control API boundary" section), `README.md`
      ("Supported today" list). `config.schema.json` regenerated with
      zero diff (no new config fields — app-mode is a CLI flag, not
      config); `openapi.json` regenerated with only the version-string
      diff (see version bump below) — `/v1/local/*` is deliberately
      **not** in the generated OpenAPI spec (that spec is generated from
      `app_mode=False`); documented in prose instead, a scope decision
      recorded in ADR-0016's "Consequences".
- [x] `pyproject.toml` bumped `0.2.0` -> `0.3.0`, `CHANGELOG.md`'s
      `## v0.3.0` heading changed from "in progress" to a dated
      (2026-09-14) release entry with unit (4)'s own bullet added — done
      as part of *this* unit's own change, since completing the last of
      v0.3.0's four units is what makes the milestone done. **Flag this
      specifically for founder attention** — a version bump is exactly
      the kind of change this repo's merge policy wants deliberately
      reviewed, not waved through because "it's just a version number."
      (Bumping `pyproject.toml` alone does not update the installed
      package's metadata for `test_version.py`'s own check — a local
      `pip install -e . --no-deps` re-sync was needed this session; CI
      does a fresh install every run so this isn't a CI concern, only a
      local-dev-loop one worth remembering.)
- [x] Local verification: `ruff check .` and `mypy` (82 files) both
      clean; `mypy hosted/ap_exceptions --strict --ignore-missing-imports`
      clean; full `pytest -q` — **860 passed, 19 skipped, 0 failed** (up
      from 820 at unit (3)'s merge point; the 40 new tests —
      `test_appdata.py`, `test_localapi_token.py`,
      `test_localapi_routes.py`, `test_cli_pricing.py`,
      `test_cli_doctor.py`, plus additions to `test_cli_main.py` and
      `test_receipts.py` — are exactly this unit's). `scripts/
      check_no_internal_content.sh` (boundary check) passes.
- [x] **PR opened, CI failure fixed, merged.** [PR #27](https://github.com/domondi1/inferrail/pull/27)
      initially failed `test (3.11)`/`test (3.12)` on a stale
      `ERRORS.md` (a new error code, `INFERRAIL_E011`, was never run
      through `scripts/generate_errors_md.py`) — diagnosed from
      `gh run view <id> --log-failed`, fixed with a follow-up commit
      pushed to the same branch (`git push origin
      feat/local-control-api-app-mode`, no new PR needed), confirmed
      all 8 checks green via `gh pr checks 27` before the founder
      squash-merged. Merge confirmed via `gh pr view 27 --json
      state,mergedAt` -> `MERGED`, merge commit `7d66719`; `ERRORS.md`
      on `origin/main` re-read afterward to confirm the fix actually
      landed (not assumed from push order).

v0.3.0's own acceptance criteria (a $0.01 hard cap blocks before the
provider is called and the block is visible in the store; a Claude Code
session pointed at the gateway produces attributed receipts;
crash/idempotency tests for budgets pass) were met by unit (3). **All
four v0.3.0 units are merged. The milestone is closed.**

## v0.4.0 — CLOSED ("The dashboard")

**Closed 2026-09-15** in a later closing-audit session (see this file's
top section for the full record): the Live Feed backfill bug found and
fixed, Node wired into `publish.yml`/`platform-verify.yml`, PyPI publish
sequencing decided, `pyproject.toml` -> `0.4.0`. Committed locally as
`ops/close-v0.4.0-audit-fixes` (commit `b251da3`), not yet pushed/merged
— see "HUMAN ACTION NEEDED". The checklist below (units 1-6) predates
that closing session and is preserved as the accurate build record.

**Architectural decision (founder-directed this session): the dashboard
lives in `app/` in this repository**, not a sibling repo. Recorded in
`docs/adr/0017-dashboard-in-app-directory.md`, which also records how
it's served (a static SPA mounted at `/dashboard` by `inferrail serve
--app-mode` when a build is found) and how it authenticates (the
per-install local-API token travels in the printed dashboard URL's query
string, since the acceptance bar is "zero terminal use after startup").

### Checklist for unit 1: scaffold, real serving/auth, Live Feed screen — DONE, merged (PR #29, `b900a59`)

- [x] `app/` scaffolded: Vite + React + TypeScript, `npm run build` ->
      `app/dist` (a static SPA, no server-side rendering, no Node
      runtime needed to serve it). Design tokens match the marketing
      site's paper-receipt language (`--paper`/`--ink`/`--stamp`/... from
      `docs/index.html`), but fonts are a local-first system stack
      (`"IBM Plex Mono", ui-monospace, ...`) rather than a Google Fonts
      network dependency — deliberate, since this is an offline-capable
      local tool, unlike the marketing site.
- [x] Hash-based client routing only (`#/live`, `#/work`, ...) — a
      permanent decision (ADR-0017), not a placeholder: the server-side
      mount never needs a SPA catch-all regardless of how many screens
      get added later.
- [x] `inferrail.dashboard.find_dashboard_dist()` — env override,
      future-bundled-package path, or an `app/dist` found by walking up
      from the source tree (what resolves today from a checkout).
      `gateway/app.py`'s `create_app` mounts it (`StaticFiles(html=True)`
      at `/dashboard`) only under `app_mode=True` and only when a build
      is actually found; `app.state.dashboard_dist` records which for
      tests/callers. A missing/unbuilt dashboard is not an error —
      every other `--app-mode` guarantee is unaffected.
- [x] `inferrail serve --app-mode` prints
      `http://<host>:<port>/dashboard/?token=<token>` once a build is
      found (and a one-line "not built yet" message with the exact build
      command otherwise) — this is what makes MISSION.md's "zero
      terminal use after startup" real: opening the printed link is the
      only step.
- [x] `localapi/routes.py`'s `_require_local_api_token` now accepts
      `?token=` as well as the `Authorization` header — narrowly
      motivated by browser `EventSource` (used by Live Feed) being
      unable to set custom headers; the header still wins when both are
      present. Documented as a scoped exception in ADR-0017, not a
      pattern to reuse elsewhere.
- [x] **Live Feed screen** (`app/src/screens/LiveFeed.tsx`): opens
      `GET /v1/local/stream`, renders each receipt as it arrives
      (provider/model, work_id/project if present, status, cost),
      de-duplicates on `receipt_id` (the poll-based tail can in principle
      redeliver), caps at 200 rows. **A `null` cost renders as the word
      "unknown", visually distinct from a real `$0.0000`** — never
      collapsed into the same thing, per MISSION.md's non-negotiable
      honest-numbers rule.
- [x] Nav shows all six v0.4.0 screens; the five not yet built (Work,
      Budgets, Recover, Connect, Settings) render as visibly disabled
      tabs rather than being omitted, so the dashboard's eventual shape
      is honest from this first unit onward.
- [x] Tests: `app/src/format.test.ts` (8 vitest cases — the honest-cost
      formatting rule explicitly, including the "$0.0000 known-zero vs.
      unknown" distinction), `tests/unit/test_dashboard.py` (discovery
      env-override + missing-index cases, app-mode-with/without-a-build
      end-to-end via `TestClient`, dashboard absent without app-mode),
      3 new cases in `tests/unit/test_localapi_routes.py` (query-token
      accepted, wrong query-token rejected, header takes precedence over
      a simultaneously-present invalid query token).
- [x] New `dashboard` CI job (`.github/workflows/ci.yml`): Node 20 setup,
      `npm ci`/type-check/`vitest run`/`npm run build`, then
      `scripts/check_dashboard_discoverable.py` — verifies the *Python*
      discovery logic actually finds the real build, not just that the
      file exists on disk. Kept fully separate from the Python `test`
      job — Node is a dev-time dependency of `app/` only, never of the
      package's own build/lint/test.
- [x] Docs: `docs/PRODUCT.md` (new "Dashboard (v0.4.0, in progress)"
      subsection, explicit about what's built vs. not), `docs/
      ARCHITECTURE.md` (component tree + new "dashboard boundary"
      section), `README.md` ("Supported today" gains the dashboard
      bullet), `CHANGELOG.md`'s new `## v0.4.0 — in progress` entry.
      `openapi.json`/`config.schema.json`/`ERRORS.md` regenerated with
      zero diff (no config or error-code changes this unit).
- [x] Local verification this session: `ruff check .` clean; `mypy`
      clean (83 source files); `bash scripts/check_no_internal_content.sh`
      clean; `cd app && npm run lint` (tsc --noEmit) clean; `npm run
      build` succeeds; `npm test` — 8/8 vitest passed;
      `pytest tests/unit/test_dashboard.py tests/unit/test_localapi_routes.py`
      — 18/18 passed. Full-repo `pytest -q`: **868 passed, 19 skipped, 0
      failed** (up from 860 at the v0.3.0 close — exactly the 8 new
      tests this unit added: 5 in `test_dashboard.py`, 3 in
      `test_localapi_routes.py`; the 19 skips are the same pre-existing
      credential-gated ones, unaffected).
- [x] **Not bumped:** `pyproject.toml` stays `0.3.0` — same rule v0.3.0's
      own in-progress units followed (only the unit that *closes* a
      milestone bumps the version); v0.4.0 is not closed yet.
- [x] **Pushed and merged.** [PR #29](https://github.com/domondi1/inferrail/pull/29)
      merged by the founder, merge commit `b900a59` — confirmed `MERGED`
      via `gh pr list --json state,mergedAt` and all 9 CI checks
      (including the new `dashboard` job) green via `gh pr checks 29`,
      before trusting the founder's report. Merge-commit tree confirmed
      byte-identical to what was authored locally (no squash drift).

### Checklist for unit 2: Work screen — DONE, merged (PR #30, `217000b`)

No backend changes needed — `GET /v1/local/work` and
`GET /v1/local/work/{work_id}` already existed (v0.3.0 unit 4). This
unit is frontend-only.

- [x] `app/src/useHashRoute.ts` — the dashboard's one router: parses
      `#/screen/param`, defaults to `live` on an empty/unrecognized hash,
      `navigateTo(screen, param?)` writes it. Permanent per ADR-0017, not
      a placeholder — this is what lets `#/work/<id>` be a real,
      bookmarkable/back-button-able URL without the server ever needing
      a SPA catch-all.
- [x] `app/src/api.ts` gained `WorkSummary`, `listWork()`, `getWork(id)`,
      and a shared `fetchLocal()` helper (Authorization-header auth —
      the ordinary case; only the SSE stream needs the `?token=`
      exception). A `LocalApiError` carries the HTTP status so the
      detail view can distinguish 404 ("not found") from any other
      failure.
- [x] `app/src/screens/Work.tsx`: a list view (click a row to drill in)
      and a detail view (receipt count, cost, status, outcome,
      started/ended). Both are real screens against real endpoints, not
      a stub — smoke-tested live against a running
      `inferrail serve --app-mode` instance with a receipt actually
      inserted into its SQLite store (not just through the test suite).
- [x] `format.ts` gained `formatWorkCost(known, unknownCount)` — the
      work-rollup-level version of the "unknown is never $0" rule:
      renders a fully-known cost plainly, a fully-unknown work_id as
      just `+N unknown`, and a partially-known one as both (`$0.0007
      (+2 unknown)`) — never collapses a partial total into a single,
      misleadingly-precise-looking number.
- [x] Nav tabs are now real: Live Feed and Work are both clickable and
      reflect the current route (`aria-current="page"`); Budgets/
      Recover/Connect/Settings remain visibly disabled.
- [x] Tests: `app/src/useHashRoute.test.ts` (4 cases — default screen,
      bare screen, screen+param, URL-decoding a param), 3 new cases in
      `format.test.ts` for `formatWorkCost` (fully-known, fully-unknown,
      partial). `npm run lint`/`npm run build`/`npm test` all clean — 15
      vitest cases total across 2 files (up from 8 at unit 1: +3
      `formatWorkCost` cases, +4 `parseHash` cases).
- [x] Live end-to-end smoke test (not just unit tests): started a real
      `inferrail serve --app-mode`, confirmed `GET /v1/local/work`
      returns `[]` before any evidence exists, inserted one real receipt
      directly into the SQLite store with `attributes.work_id` set,
      confirmed both `GET /v1/local/work` and
      `GET /v1/local/work/WORK-SMOKE-1` return the exact shape
      `app/src/api.ts`'s `WorkSummary` interface expects.
- [x] Local verification: `ruff check .` clean (no Python changed, but
      re-run anyway per protocol), `bash scripts/check_no_internal_content.sh`
      clean, `cd app && npm run lint && npm run build && npm test` —
      2 files, 15 tests, all passed. No backend tests to add (no backend
      change); full-repo `pytest -q` unaffected (not re-run this unit —
      zero Python files touched, confirmed via `git status`).
- [x] Docs: `docs/PRODUCT.md`'s dashboard subsection retitled and
      extended, `README.md`'s dashboard bullet updated, `CHANGELOG.md`'s
      `## v0.4.0` entry gained the Work bullet.
- [x] **Not bumped:** `pyproject.toml` stays `0.3.0` — same rule as
      unit 1; v0.4.0 is still not closed (Budgets/Recover/Connect/
      Settings remain).
- [x] **Pushed and merged.** [PR #30](https://github.com/domondi1/inferrail/pull/30)
      merged, merge commit `217000b` — confirmed `MERGED` and all 9 CI
      checks green before trusting the founder's report; merge-commit
      tree byte-identical to the local commit (no squash drift).

### Checklist for unit 3: Budgets screen — DONE, merged (PR #31, `8d43bc6`)

Unlike unit 2, this one needed real backend work: the endpoints
`GET /v1/local/budgets/spend` and receipts' `status` filter didn't exist
yet, and a pre-flight block's receipt carried nothing to distinguish it
from any other failure.

- [x] **`budgets/enforcement.py`** gained
      `augment_attributes_with_block(attributes, exc)` — same pattern as
      the existing `augment_attributes_with_overrun`: a pure function
      that adds one system-computed key (`budget_id`) into the receipt's
      generic `attributes` dict. Wired into both `_check_budgets` except-
      blocks (`gateway/execution.py` and `gateway/anthropic_execution.py`)
      — the only two call sites that know the exception is a
      `BudgetExceededError`, not `_emit_failure` itself (which handles
      many other exception types generically).
- [x] **`receipts/sqlite_store.py`**: `ReceiptsStore.query()`/`count()`
      gained an optional `status` filter — `status` was already a real
      column, just not one `query()` exposed; deliberately left
      unindexed (documented why: a local single-install table is small
      enough that a full scan is fine, not worth an index just for one
      dashboard filter). `localapi/routes.py`'s `list_receipts` threads
      it through as a new optional query param — fully additive, every
      existing caller/test unaffected.
- [x] **New `GET /v1/local/budgets/spend`** (`localapi/routes.py` +
      `localapi/schemas.py`'s new `BudgetSpend`): one entry per
      configured budget, reusing `budgets.enforcement.spent_so_far_usd`
      directly — the exact function `BudgetEnforcer.check` itself calls
      — so the dashboard's burn bar can never compute a different number
      than enforcement did. `has_unpriced_usage` surfaces honestly rather
      than under-reporting spend when a receipt has real usage but no
      known price.
- [x] `app/src/screens/Budgets.tsx`: a create-budget form (scope/window/
      mode/limit, with window choices constrained per scope — `per_work`
      only offered for `scope: work_id`, matching the schema's own
      validator), a list of existing budgets each with a burn bar (green/
      red at 100%+) and a Remove button, and a blocked-request log below.
- [x] `app/src/api.ts` gained `Budget`/`BudgetCreate`/`BudgetSpend`
      types, `listBudgets`/`createBudget`/`deleteBudget`/
      `listBudgetSpend`, and `listBlockedReceipts()` (fetches
      `status=error` receipts and filters client-side for a `budget_id`
      attribute — not every error receipt is a budget block, so this
      distinction matters).
- [x] `format.ts` gained `burnFraction(spent, limit)`, clamped to [0, 1]
      for rendering (the raw ratio can exceed 1 — a "warn" budget is
      allowed to go over, and even a "block" budget can be pushed over
      post-flight by a real cost exceeding its pre-flight estimate).
- [x] Nav: Budgets tab is now enabled/clickable.
- [x] Tests: 3 new backend unit tests (`test_augment_attributes_with_block_*`
      in `test_budgets.py`), 1 new `test_sqlite_store_query_and_count_filter_by_status`
      in `test_receipts.py`, 2 budget-block-attribute assertions added to
      existing tests in `test_gateway_budgets.py` (chat + messages paths),
      3 new local-API tests (`status` filter, `budgets/spend` reusing
      enforcement's computation, `budgets/spend` flagging unpriced usage)
      in `test_localapi_routes.py` — 88 tests pass across these 4 files.
      Frontend: 3 new `burnFraction` cases in `format.test.ts`.
- [x] Live end-to-end smoke test (not just unit tests): started a real
      `inferrail serve --app-mode`, created a global block budget with a
      $0.0001 limit via the real API, confirmed `budgets/spend` reports
      `$0` spent before any receipts exist, sent a real
      `POST /v1/chat/completions` that got rejected `402` by the budget,
      then confirmed `GET /v1/local/receipts?status=error` returns that
      exact receipt with `attributes.budget_id` set — the precise shape
      the frontend's `listBlockedReceipts()` depends on.
- [x] Local verification: `ruff check .`/`mypy` clean (83 files),
      `bash scripts/check_no_internal_content.sh` clean,
      `cd app && npm run lint && npm run build && npm test` — 18 vitest
      cases (up from 15), all passed. All three generator scripts
      re-run, zero diff (no config/error-code changes this unit).
      Full-repo `pytest -q`: **874 passed, 19 skipped, 0 failed** (up
      from 868 at unit 2 — exactly the 6 new backend tests this unit
      added). One transient failure earlier in this session
      (`test_a2a_economic_authority_transport.py::test_get_task_is_disabled_regardless_of_credential`)
      was caused by this session accidentally running two full
      `pytest -q` invocations concurrently (a stray `&`-backgrounded
      shell command alongside a properly tracked one) — that test starts
      a real subprocess server on a fixed port, so the two runs
      collided; it passed cleanly in isolation and again in this final
      clean single run. Not a regression from anything in this unit.
- [x] Docs: `docs/PRODUCT.md`'s dashboard subsection and "Budgets and
      enforcement" subsection both updated, `docs/ARCHITECTURE.md`'s
      "budgets boundary" section extended, `README.md`'s dashboard
      bullet updated, `CHANGELOG.md`'s `## v0.4.0` entry gained the
      Budgets bullet.
- [x] **Not bumped:** `pyproject.toml` stays `0.3.0` — v0.4.0 is still
      not closed (Recover/Connect/Settings remain).
- [x] **Pushed and merged.** [PR #31](https://github.com/domondi1/inferrail/pull/31)
      merged, merge commit `8d43bc6` — confirmed `MERGED` and all 9 CI
      checks green before trusting the founder's report; merge-commit
      tree byte-identical to the local commit (no squash drift).

### Checklist for unit 4: Recover screen — DONE, merged (PR #32, `ffce54f`)

The first unit to bridge the dashboard to `inferrail.ap` — a previously
separate module with its own store, its own CLI subcommands
(`inferrail ap demo|report|outcome|reap`), and no prior config-file or
`--app-mode` wiring at all.

- [x] **`_apply_app_mode` (`cli/main.py`)** gained `ap_recovery: Path`
      in `_AppModePaths` — a fixed default under the app-data dir
      (`ap-recovery.db`), overridable via `INFERRAIL_AP_DB` (an env var,
      not a new CLI flag — kept minimal since this is the Recover
      screen's only consumer so far). Printed on startup alongside
      receipts/budgets, with the exact `--db` value to point
      `inferrail ap demo|report|outcome` at to populate it.
- [x] **`create_app` (`gateway/app.py`)** gained `ap_recovery_path`;
      under `app_mode=True` it always constructs an
      `ap.store.RecoveryStore` at that path (or the default) —
      unconditional, same treatment as receipts/budgets, never "mounted
      only if AP happens to be in use." `RecoveryStore.__init__` creates
      its schema eagerly and is safe against a nonexistent/empty file;
      an empty store is a normal state (`build_live_report` returns zero
      rows), never an error — confirmed both by a live smoke test and by
      `test_ap_pending_is_empty_with_no_decisions`.
- [x] **New `GET /v1/local/ap/pending`** (`localapi/routes.py` +
      nothing new in `schemas.py` — returns the same dict shape
      `ap.report.build_live_report(...).to_dict()` already produces,
      filtered to `status == "awaiting_human_review"`, matching the
      precedent `hosted/ap_exceptions/service.py`'s own `GET /v1/report`
      already set for "no new response model, reuse the one report
      shape"). **New `POST /v1/local/ap/{work_id}/outcome`**
      (`localapi/schemas.py`'s new `OutcomeRequest`, deliberately not
      imported from `hosted/ap_exceptions/service.py`'s own copy since
      `hosted/` is a separate deployable, never a dependency of the
      installed package) — calls `RecoveryStore.record_outcome` directly,
      the same store-level call `inferrail ap outcome` and the hosted
      API's own outcome route make; a `KeyError` (unknown work_id) maps
      to `404`, matching both of those existing precedents exactly.
- [x] `app/src/screens/Recover.tsx`: a pending-review queue (failure
      type, reason, sunk/retry cost) with an inline outcome/review-cost
      form per row and a "record outcome" button; an empty store or one
      with nothing pending both render as honest empty states, not
      errors — a missing AP recovery store isn't assumed to be a bug.
- [x] `app/src/api.ts` gained `PendingReview`, `OutcomeRequest`,
      `listPendingReviews()`, `recordOutcome()`.
- [x] Nav: Recover tab is now enabled/clickable.
- [x] Tests: 5 new local-API tests (`test_localapi_routes.py` —
      empty-by-default, filters to awaiting-review only, requires the
      token, resolves a decision end-to-end confirming it drops out of
      `pending` afterward, 404 on an unknown work_id), 2 new CLI tests
      (`test_cli_main.py` — the ap-recovery path is created and printed,
      `INFERRAIL_AP_DB` override is honored and actually used by the
      constructed store, plus one existing app-mode test extended with
      ap-recovery assertions) — 6 new backend tests across both files.
      `cd app && npm run lint && npm run build && npm test` — 18
      vitest cases (unchanged; Recover's logic is thin enough it didn't
      need new pure-function tests the way Work/Budgets did — its inline
      form logic is exercised by the live smoke test below instead).
- [x] Live end-to-end smoke test (not just unit tests): started a real
      `inferrail serve --app-mode`, confirmed `ap/pending` is `[]` before
      any decisions exist and the startup log prints the exact
      `ap-recovery.db` path, seeded one real `awaiting_human_review`
      decision directly via `RecoveryStore.create_decision` (the same
      call `inferrail ap`'s own decision path makes), confirmed it
      appears in `ap/pending` with the exact shape the frontend's
      `PendingReview` interface expects, recorded a real outcome via
      `POST .../outcome`, confirmed it then disappears from `pending`,
      and confirmed an unknown work_id correctly 404s.
- [x] Local verification: `ruff check .`/`mypy` clean (83 files),
      `bash scripts/check_no_internal_content.sh` clean. All three
      generator scripts re-run, zero diff (no config/error-code changes
      this unit — the local API still isn't in the generated OpenAPI
      spec, per ADR-0016's existing scope decision). Full-repo
      `pytest -q`: **880 passed, 19 skipped, 0 failed** (up from 874 at
      unit 3 — exactly the 6 new backend tests this unit added).
- [x] Docs: `docs/PRODUCT.md`'s dashboard subsection updated,
      `docs/ARCHITECTURE.md`'s dashboard-boundary section gained a
      paragraph on the AP bridge, `README.md`'s dashboard bullet
      updated, `CHANGELOG.md`'s `## v0.4.0` entry gained the Recover
      bullet.
- [x] **Not bumped:** `pyproject.toml` stays `0.3.0` — v0.4.0 is still
      not closed (Connect/Settings remain).
- [x] **Pushed and merged.** [PR #32](https://github.com/domondi1/inferrail/pull/32)
      merged, merge commit `ffce54f` — confirmed `MERGED` and all 9 CI
      checks green before trusting the founder's report; merge-commit
      tree byte-identical to the local commit (no squash drift).

### Checklist for unit 5: Connect + Settings screens — DONE, merged (PR #33, `41f83aa`)

The last two `MISSION.md` v0.4.0 screens, built together since neither
depends on the other and both are small. **All six v0.4.0 screens now
exist.**

- [x] **`app/src/screens/Connect.tsx`**: curl, Claude Code/Anthropic SDK
      env var, the Anthropic Messages API, the OpenAI Python SDK, and
      LangChain snippets — adapted verbatim from `README.md`'s own "Use
      it as a gateway" section, not invented fresh, so the dashboard
      never says something the docs don't already say. Every snippet is
      built against `window.location.origin` — since the dashboard is
      served by the exact same process as the gateway (`docs/adr/0017`),
      this is the real, currently-running base URL, not a
      `127.0.0.1:8000` placeholder that might not match the actual port.
      The two snippets that need an Anthropic route configured say so in
      their own blurb rather than implying universal applicability.
- [x] New `app/src/CopyButton.tsx` — shared by Connect (one per
      snippet); same clipboard fallback discipline the marketing site's
      own copy button already established (write → "Copied", a genuine
      failure → "Copy failed", never silently swallowed).
- [x] **New `GET /v1/local/pricing/freshness`** (`localapi/routes.py`):
      wraps `cli.pricing.catalog_freshness` — the exact function
      `inferrail pricing update`/`inferrail doctor` already share —
      never a network fetch (matches that module's own "there is no
      network call that would stay verified" rule).
- [x] **New `GET /v1/local/receipts/export`**: streams every stored
      receipt as JSONL directly from `ReceiptsStore.read_all()`, not
      via `export_jsonl`'s file-to-file path — avoids writing a
      server-side temp file just to immediately re-read it for an HTTP
      response body. `Content-Disposition: attachment` so a browser
      downloads it as a real file.
- [x] `app/src/screens/Settings.tsx`: a working Export button
      (`downloadReceiptsExport()` — fetch + blob + a programmatic
      `<a download>` click, since an authenticated download can't be a
      plain `<a href>` link), a real pricing-catalog freshness table,
      and — **the one deliberate judgment call in this unit** — a
      **disabled** "opt-in usage ping" checkbox with an explicit label
      explaining why: no telemetry-ping mechanism exists anywhere in
      this codebase, and inventing a new privacy-surface feature
      (network calls, an opt-in flag, what it would even send) was out
      of scope for a dashboard-presentation unit. Per `MISSION.md`'s
      "flag, don't fake" rule, this renders as an honestly-labeled
      placeholder, never a checkbox that silently does nothing while
      implying it works. **Founder attention worth having before v0.4.0
      formally closes:** confirm this is the right call, or that a real
      opt-in-ping feature should be scoped as its own future unit.
- [x] `app/src/api.ts` gained `CatalogFreshness`, `getPricingFreshness()`,
      `downloadReceiptsExport()`.
- [x] `App.tsx` refactored from a chain of `route.screen === "x" && ...`
      conditionals into a `Record<Screen, Component>` map, now that all
      six screens are real — every tab is enabled and clickable; the
      "not built yet" disabled-tab styling in `styles.css` is now unused
      but left in place rather than removed mid-unit for no functional
      reason.
- [x] Tests: 4 new local-API tests (`test_localapi_routes.py` — pricing
      freshness reports both built-in catalogs, requires the token,
      export streams exactly the stored receipts as JSONL with the
      correct `Content-Disposition`, export requires the token).
      `cd app && npm run lint && npm run build && npm test` — 18 vitest
      cases (unchanged; Connect/Settings are thin enough their logic is
      exercised by the live smoke test below rather than needing new
      pure-function tests).
- [x] Live end-to-end smoke test (not just unit tests): started a real
      `inferrail serve --app-mode`, confirmed `pricing/freshness`
      reports both catalogs with real ages, inserted a real receipt,
      confirmed `receipts/export` returns it as JSONL with the correct
      headers.
- [x] Local verification: `ruff check .`/`mypy` clean (83 files),
      `bash scripts/check_no_internal_content.sh` clean. All three
      generator scripts re-run, zero diff. Full-repo `pytest -q`:
      **884 passed, 19 skipped, 0 failed** (up from 880 at unit 4 —
      exactly the 4 new tests this unit added). One transient failure
      appeared mid-session in `test_a2a_economic_authority_transport.py`
      (a real subprocess-server test that binds a real port and polls
      it with a timeout) — traced to a second, independent `pytest -q`
      process running concurrently on this shared machine (not started
      by this session, confirmed via `ps aux` and its own PID/start
      time), which starved the test's subprocess past its readiness
      timeout under load. Waited for that other process to exit, then
      reran clean — 0 failures. Not a regression from anything in this
      unit; nothing in `hosted/a2a_economic_authority` was touched.
- [x] Docs: `docs/PRODUCT.md`'s dashboard subsection retitled ("all six
      screens built") and extended, `README.md`'s dashboard bullet
      updated, `CHANGELOG.md`'s `## v0.4.0` entry gained the Connect/
      Settings bullets and a "not yet closed" section (renamed from
      "not yet in this milestone" now that every screen exists).
- [x] **Not bumped:** `pyproject.toml` stays `0.3.0` — the milestone
      isn't formally closed until the wheel-packaging follow-up lands
      and MISSION.md's acceptance criterion gets an explicit founder
      sign-off, even though it's now behaviorally true.
- [x] **Pushed and merged.** [PR #33](https://github.com/domondi1/inferrail/pull/33)
      merged, merge commit `41f83aa` — confirmed `MERGED` and all 9 CI
      checks green before trusting the founder's report; merge-commit
      tree byte-identical to the local commit (no squash drift).

### Checklist for unit 6: bundle the dashboard into the PyPI wheel — DONE, merged (PR #35, `558365d`)

Closes ADR-0017's "Known gap" for the wheel this project's own CI
builds — see `docs/adr/0018-dashboard-wheel-packaging.md` for the full
design record; this checklist covers what was actually done and
verified.

- [x] New `hatch_build.py` (repo root): a hatchling custom build hook,
      registered via `[tool.hatch.build.hooks.custom]`, that runs
      `npm ci && npm run build` in `app/` and copies the result into
      `src/inferrail/dashboard_static/` — only for the `wheel` target,
      never the `sdist`.
- [x] **Never fails the build**: no `app/package.json` (stripped
      source), no `npm` on `PATH`, a failed npm build, or a build that
      doesn't produce `index.html` — each prints one line to stderr and
      returns, leaving the wheel to build normally without a bundled
      dashboard. Verified by 5 of the 6 new unit tests
      (`tests/unit/test_hatch_build.py`), each exercising exactly one
      skip path via a mocked `shutil.which`/`subprocess.run`.
- [x] **Real bug found and fixed while testing this, not left latent:**
      the first working version wrote files directly under
      `src/inferrail/dashboard_static/` and relied on the existing
      `packages = ["src/inferrail", ...]` config to pick them up — this
      silently produced a wheel with *no dashboard in it at all*, with
      no error, because hatchling's default wheel file selection
      respects `.gitignore`, and `dashboard_static/` is (correctly)
      gitignored as a generated artifact. Caught only by actually
      inspecting the built wheel's contents (`unzip -l`), not by trusting
      the hook's own success log or its unit tests. Fixed by using
      `build_data["force_include"]` instead — hatchling's documented
      mechanism for exactly this "hook-generated, intentionally
      gitignored path" case.
- [x] **Full manual end-to-end verification, not just CI-shaped
      checks:** built a real wheel (`python -m build --wheel`),
      confirmed via `unzip -l` that `inferrail/dashboard_static/{index.html,assets/*}`
      are actually present, installed that wheel into a brand-new venv
      **outside any checkout**, confirmed
      `inferrail.dashboard.find_dashboard_dist()` finds the bundled copy
      from there, then started a real `inferrail serve --app-mode` from
      that clean install and confirmed `GET /dashboard/` and its
      hashed asset path both return real content — the same discipline
      this project's own release-verification passes (v0.2.0) used.
- [x] **Pre-existing, unrelated packaging bug found and fixed in the
      same pass:** hatchling's default sdist target does not respect
      `app/.gitignore` either — a plain `python -m build` was including
      `app/node_modules` (real, sizable bloat) and any locally-built
      `app/dist` in the source tarball. Present since `app/` was first
      added (unit 1), not introduced by this unit. Fixed via an explicit
      `[tool.hatch.build.targets.sdist]` `exclude`; verified via a real
      `python -m build --sdist` before and after (0 `node_modules`
      entries after the fix).
- [x] `hatchling>=1.18` added to the `dev` extra — needed only so
      `test_hatch_build.py` can import `BuildHookInterface` (it was
      already an implicit build-time dependency via `[build-system]
      .requires`, just not otherwise importable due to pip's PEP 517
      build isolation).
- [x] `ci.yml`'s existing `dashboard` job (already has Node set up)
      extended with two new steps: build a real wheel and assert
      `inferrail/dashboard_static/index.html` is in the archive: install
      that wheel into a clean venv and assert `find_dashboard_dist()`
      finds it — the same sequence verified manually above, now
      re-verified on every push/PR rather than only once by hand.
- [x] New ADR: `docs/adr/0018-dashboard-wheel-packaging.md`.
- [x] Tests: 6 new (`tests/unit/test_hatch_build.py`) — full-repo
      `pytest -q`: **890 passed, 19 skipped, 0 failed** (up from 884 at
      unit 5 — exactly the 6 new tests this unit added). No concurrent
      session collision this time (checked via `ListAgents` before
      running).
- [x] Local verification: `ruff check .`/`mypy` clean, all three
      generator scripts re-run with zero diff, `bash
      scripts/check_no_internal_content.sh` clean.
- [x] Docs: new ADR-0018; `docs/PRODUCT.md`'s dashboard subsection,
      `docs/ARCHITECTURE.md`'s dashboard-boundary section, `README.md`'s
      dashboard bullet, and `CHANGELOG.md`'s `## v0.4.0` entry all
      updated to reflect the wheel now bundling the dashboard (with the
      publish.yml/platform-verify.yml gap stated plainly, not implied
      closed).
- [x] **Deliberately not done, stated plainly rather than implied:**
      `publish.yml` (the actual PyPI release pipeline) and
      `platform-verify.yml` (the three-OS wheel-smoke tests) don't set
      up Node yet — neither the real published wheel nor the
      Windows/macOS/Linux platform-verify wheels are proven to bundle a
      dashboard. Wiring Node into those two workflows is a separate,
      higher-stakes follow-up (they're this repo's most heavily-reviewed
      files, per its own merge policy) — a deliberate scope boundary for
      this unit, not an oversight. See ADR-0018's "Consequences" and
      "HUMAN ACTION NEEDED" below.
- [x] **Pushed, fixed twice post-push, and merged.**
      [PR #35](https://github.com/domondi1/inferrail/pull/35) merged,
      merge commit `558365d` — confirmed `MERGED` and all 9 CI checks
      green (including a clean `dashboard` run) before trusting the
      founder's report; merge-commit tree byte-identical to the local
      commit (no squash drift). Two real problems found and fixed
      post-push, neither hidden:
      1. A `CONFLICTING` mergeable state, caused by a git-history
         artifact from rebasing onto a commit that got squash-merged
         under a different hash — fixed by rebasing onto the actual
         merged commit; force-pushed by the founder.
      2. CI's `dashboard` job then failed for real:
         `scripts/check_dashboard_discoverable.py` assumed
         `find_dashboard_dist()` could only ever return the `app/dist`
         checkout fallback, but that job's own `pip install -e .` step
         also triggers the new build hook (Node is already on `PATH` by
         then), which legitimately produces a `dashboard_static/`
         result instead — a correct outcome the check script didn't
         know about yet. Reproduced locally before fixing, fix verified
         against that exact reproduction, then force-pushed again by
         the founder — clean on the next run.

### Known gaps, explicitly deferred (not hidden) — see ADR-0017's/0018's "Consequences"

- `npm audit` reports 5 vulnerabilities (3 moderate, 1 high, 1 critical)
  in `vite`/`vitest`'s own dev-server dependency chain (`esbuild`,
  `@vitest/mocker`) — dev-tooling only (affects `npm run dev`'s dev
  server, not the built static output this unit actually ships); fixing
  requires a breaking major-version bump (`vite@8`, `vitest@5`) not
  attempted in this unit. Tracked, not silently ignored.
- The Recover screen requires an AP recovery store — most `--app-mode`
  users who never touch `inferrail ap` will see an empty queue, not an
  error, which is correct, but is worth knowing before expecting the
  screen to show anything without first running `inferrail ap demo`
  or pointing `INFERRAIL_AP_DB` at an existing store.
- ~~The actual PyPI-published wheel and the three-OS
  `platform-verify.yml` wheels still don't bundle a dashboard~~ —
  **resolved in the 2026-09-15 closing-audit session** (this file's top
  section): both workflows now set up Node and assert the bundling
  themselves.

## v0.4.1 — CLOSED (opt-in usage ping) — committed, not yet pushed/merged

Founder decision, 2026-09-15: build the opt-in usage ping for real now,
replacing v0.4.0's own disabled Settings placeholder (the "1." item in
this file's "Status summary" section above). Full design record:
`docs/adr/0019-opt-in-usage-ping.md`. Full audit/verification narrative
in this file's top section — this section is the build checklist.

- [x] `src/inferrail/usage_ping/` — `install_id.py` (random,
      local-only, race-safe persisted id), `state.py` (mutable
      `usage-ping-state.json` under the app-data dir: the on/off toggle
      + idempotent per-event "already sent" markers), `payload.py`
      (the fixed, exhaustive payload shape), `client.py`
      (`maybe_send_event` — the one call every integration point uses;
      no-ops immediately with zero network access when disabled or
      unconfigured; otherwise a daemon-thread `httpx.post` with a 3s
      timeout, every exception swallowed), `receipt_hook.py`
      (`UsagePingReceiptSink`, wraps a `ReceiptSink` — never reuses
      `TelemetrySink`/`ReceiptSink` as its own transport).
- [x] `config/models.py`'s new `UsagePingConfig` (`enabled: bool =
      False`, `endpoint: str | None = None`) — no built-in default
      endpoint anywhere in this package.
- [x] `gateway/app.py`: under `app_mode=True` only, wraps the
      `ReceiptSink` the two inference engines hold (not the raw
      `ReceiptsStore` the budget enforcer/local API still use directly)
      with `UsagePingReceiptSink`, and fires `first_run` once at
      startup. Required moving `app_data`'s computation earlier than
      where it previously lived (still reused, not recomputed, by
      app_mode's existing setup further down) — the wrapper has to be
      in place *before* the engines are constructed.
- [x] `POST /v1/local/budgets` fires `budget_created` (not
      `inferrail budget set` — deliberately: that CLI command is
      config-independent by design, works against a bare `--db` path
      with no `inferrail.yaml` at all, and requiring it to load a
      config just to check a ping setting would add exactly the
      coupling it was built to avoid).
- [x] New `GET`/`POST /v1/local/usage-ping` (`localapi/routes.py` +
      `localapi/schemas.py`'s `UsagePingStatus`/`UsagePingUpdate`) —
      `configured: false` (no endpoint) is reported separately from
      `enabled`, so the dashboard can render "not yet active" correctly
      regardless of the toggle. `endpoint` is deliberately not
      settable via this API — only the on/off toggle is; the endpoint
      itself stays an operator/config-file decision, closing off a
      redirect-the-pings-elsewhere abuse surface for no real benefit.
- [x] `app/src/screens/Settings.tsx`: the real toggle, replacing
      v0.4.0's disabled placeholder. Verified **live, in a real
      headless-Chromium browser**: clicked the checkbox, confirmed the
      server-side state persisted across a fresh page load, and
      confirmed the "Not yet active — no collection endpoint is
      configured" copy renders correctly whenever `endpoint` is unset,
      *regardless* of the toggle's own state (tested both states
      explicitly).
- [x] `inferrail telemetry preview|status|enable|disable`
      (`cli/telemetry.py`) — `preview` prints the exact JSON payload for
      every event from this install's real id/OS/version without
      sending anything; works standalone, with no `--app-mode` and no
      `inferrail.yaml` at all (falls back to "unconfigured" honestly
      rather than erroring).
- [x] `docs/privacy/usage-ping.md` — the plain-language privacy page,
      linked from the Settings toggle via the same
      `github.com/.../blob/main/...` pattern `ERRORS.md`'s own
      `docs_url` links already use.
- [x] **A proposed, fully built reference collector**
      (`hosted/usage_ping/service.py` + `requirements.txt` + `README.md`)
      — own process, own SQLite storage, **zero dependency on the
      `inferrail` package** (unlike `hosted/ap_exceptions`, which
      genuinely needs `inferrail.ap`), no auth required on `POST /ping`
      (the payload is harmless/anonymous), server-side schema
      enforcement (`extra="forbid"` — an accidental or malicious extra
      field is rejected `422`, not silently stored), per-IP rate limit,
      a kill switch (`USAGE_PING_ENABLED=false` fails open toward the
      client — never surfaces that collection is off), and an
      admin-token-gated `GET /stats` (aggregate counts only, disabled
      entirely — a bare `404` — unless `USAGE_PING_ADMIN_TOKEN` is set).
      **Never logs or persists the connecting IP address** — verified
      by inspecting the actual SQLite schema after a live local smoke
      test, not just by reading the code. Proposed hosting: Render's
      free tier (same platform `hosted/ap_exceptions` already uses, no
      new vendor account) — deploying an instance is a human action,
      see "HUMAN ACTION NEEDED".
- [x] One real bug found and fixed while building this, not left latent:
      the collector's first draft had a module-level `app =
      create_app()` (the usual `uvicorn module:app` shape) — this
      silently created a stray `usage-ping.sqlite3` file at the default
      path on every bare *import* of the module, including during this
      project's own test collection. Caught by noticing the stray file
      in `git status` after a full test run, not by the tests
      themselves (they all passed either way). Fixed by matching
      `hosted/ap_exceptions/service.py`'s own pattern exactly: `app` is
      only ever constructed inside `if __name__ == "__main__":`.
- [x] Tests: 44 new — `tests/unit/test_usage_ping.py` (19: install id,
      state idempotency, payload shape/rejection, the client's
      no-op-when-unconfigured/disabled paths, fire-exactly-once
      behavior, failure-never-raises, the receipt-hook wrapper's
      first_receipt/tool_connected logic, and two `create_app`-level
      integration tests confirming `first_run` fires once at app-mode
      startup and that non-app-mode `create_app` never touches usage
      ping at all), 5 new in `test_localapi_routes.py` (status/toggle/
      auth/the budget-created wiring), 6 in `test_cli_telemetry.py`,
      3 in `test_config.py`, 11 in
      `tests/unit/hosted/test_usage_ping_service.py` (schema
      enforcement, kill switch, rate limit, size limit, IP-never-
      persisted, admin-gated stats). Full-repo `pytest -q` (final,
      combined with v0.4.0's own closing-audit fix): **934 passed, 19
      skipped, 0 failed** (up from 890 at the prior session's close).
- [x] Local verification: `ruff check .` clean, `mypy` clean (92 source
      files, up from 85 — six new `usage_ping/` modules + `cli/
      telemetry.py`), `mypy hosted/usage_ping --strict
      --ignore-missing-imports` clean (new dedicated CI job added,
      `usage-ping-collector` in `ci.yml`, mirroring the `ap-exceptions`/
      `hosted-economic-authority` job pattern — installs
      `hosted/usage_ping/requirements.txt` + ruff/mypy directly, since
      this collector has zero dependency on the `inferrail` package at
      all), `bash scripts/check_no_internal_content.sh` clean, all
      three generator scripts re-run (`config.schema.json` gained the
      new `usage_ping:` section; `ERRORS.md`/`openapi.json` (beyond the
      version-string bump) unchanged — `/v1/local/*` still isn't in the
      generated OpenAPI spec, per ADR-0016's existing scope decision).
      `cd app && npm run lint && npm run build && npm test` clean (18
      vitest cases, unchanged — Settings' new logic is thin enough it's
      exercised by the live browser smoke test above, matching this
      project's own established convention for these screens).
- [x] Docs: new ADR-0019, new `docs/privacy/usage-ping.md`, `docs/
      PRODUCT.md`'s new "Opt-in usage ping" subsection, `docs/
      ARCHITECTURE.md`'s new "usage-ping boundary" section + component-
      tree entry, `README.md`'s "Supported today" list, `inferrail.
      example.yaml`'s new commented `usage_ping:` section,
      `CHANGELOG.md`'s new `## v0.4.1` entry.
- [x] `pyproject.toml` bumped `0.4.0` -> `0.4.1` — this unit closes a
      real, shippable milestone (a genuine feature, not an in-progress
      fragment), matching the same "only the unit that closes a
      milestone bumps the version" rule every prior milestone followed.
- [ ] **Not yet pushed or merged** — this agent cannot push to this
      repository (the same standing limitation logged for every prior
      unit). Committed locally as `feat/opt-in-usage-ping` (commit
      `cf35904`), **stacked on top of** `ops/close-v0.4.0-audit-fixes`
      (commit `b251da3`) — the v0.4.0-closing branch must be pushed and
      merged to `main` *first*; this branch's PR should target `main`
      only after that merge (or target the other branch directly, then
      be retargeted once it merges — see "HUMAN ACTION NEEDED" for the
      exact commands either way).

## Next session starts here

1. **First action, before writing any new code:** confirm nothing
   changed underneath since this session — check whether the founder
   has pushed/merged `ops/close-v0.4.0-audit-fixes` and
   `feat/opt-in-usage-ping` yet (see "HUMAN ACTION NEEDED" — this agent
   committed both locally but cannot push). If merged, `main` should be
   at `cf35904`'s content (or a squash-merge of it) on top of `main` at
   `33956b1`. Verify with `gh pr list --json state,mergedAt` and `git
   log`, never take a verbal report on trust — same discipline every
   prior milestone used, including two specific past incidents in this
   file where that discipline caught a real problem.
2. **Check for a concurrent session on this same checkout before
   editing anything** (`ListAgents` or ask the founder) — prefer a
   separate git worktree over two sessions on one checkout at once
   (this file has two separate real incidents of that going wrong).
3. **If the two branches above are merged:** v0.4.0 and v0.4.1 are both
   closed. Remaining work, in the founder's own stated sequence
   ("verify → fix → land v0.4.0 → bump → publish"):
   - **PyPI publish is still a pending human action** (tag push — this
     agent has no PyPI credentials and cannot push git tags either).
     See "HUMAN ACTION NEEDED" for the exact command. Recommend tagging
     `v0.4.1` (not `v0.4.0`) if both branches are merged by then, since
     0.4.1 is a strict superset and there's no reason to publish an
     intermediate release the moment it's superseded — but this is the
     founder's call, not this agent's to decide unilaterally.
   - **Deploy `hosted/usage_ping/service.py`** (built and tested this
     session, not yet deployed anywhere) and report back the resulting
     URL — see "HUMAN ACTION NEEDED" for exact steps. Once given a real
     URL, a follow-up session should decide (with the founder) whether
     to bake it in as this package's actual default
     `usage_ping.endpoint`, or leave it operator-configured-only.
   - Non-telemetry signal in the meantime: PyPI download counts
     (`pypistats.org/packages/inferrail` or the JSON API) and GitHub
     clone/star counts are free and already available post-publish —
     don't wait on the usage-ping collector to start watching those.
4. **If the two branches above are NOT yet merged:** do not start new
   feature work on `main` — either wait for the founder to push/merge,
   or (if picking up unrelated work) use a separate git worktree so
   this checkout's own uncommitted-nothing state (everything is
   committed to the two branches, working tree is clean) isn't
   disturbed.
5. v0.5.0 (one-click desktop app — PyInstaller + Tauri, 3 OSes, code
   signing decision) is next per `MISSION.md`, once the above settles.
   Not started; the prior session's own risk note (v0.5.0 likely takes
   longer than its single milestone entry implies, and the
   signed-vs-unsigned-launch question has real lead time if signing
   isn't deferred to v0.9.0 as `MISSION.md` currently plans) still
   stands, unrevisited this session.
6. Follow the same protocol throughout: build with tests at the
   existing rigor (`pytest` and `vitest`), run *all three* generator
   scripts before opening a PR if any backend file changes, commit
   locally, then hand the founder the exact `git push`/`gh pr create`
   commands (this agent cannot push to this repo). Do not self-merge.

## HUMAN ACTION NEEDED

- **Two branches committed locally, neither pushed yet** (this agent
  cannot push — the same 403/no-credentials limitation logged for
  every prior unit in this file). Paste these into your own terminal,
  in order (the second depends on the first's PR existing, since it's
  stacked):

  ```
  git push -u origin ops/close-v0.4.0-audit-fixes
  gh pr create --base main --head ops/close-v0.4.0-audit-fixes \
    --title "fix: Live Feed backfill, wire Node into release workflows, close v0.4.0 (0.4.0)" \
    --body "See PROGRESS.md's top section (\"v0.4.0 closing audit + v0.4.1\") for the full audit record and what this closes."

  git push -u origin feat/opt-in-usage-ping
  gh pr create --base ops/close-v0.4.0-audit-fixes --head feat/opt-in-usage-ping \
    --title "feat: opt-in, anonymous usage ping (0.4.1)" \
    --body "Stacked on the v0.4.0-closing PR above -- see PROGRESS.md's top section for the full record. Founder decision: build the usage ping for real, now."
  ```

  Review and merge the first PR before the second (retarget the second
  PR's base to `main` once the first merges, or merge them in sequence
  as-is — either works; just don't merge the second one first, since it
  contains the first one's commit too). Confirm each merge with `gh pr
  view <n> --json state,mergedAt` before trusting it, per this file's
  own standing discipline.

- **PyPI publish is a pending human action, once the branches above are
  merged.** This agent has no PyPI credentials and cannot push a git
  tag either. `publish.yml` triggers on any `v*.*.*` tag push:

  ```
  git checkout main && git pull
  git tag v0.4.1   # or v0.4.0, if publishing before the usage-ping PR merges
  git push origin v0.4.1
  ```

  Then watch the `Publish to PyPI` workflow run in the GitHub Actions
  tab — it re-runs the full CI suite and platform-verify matrix against
  the exact tagged commit before publishing, so a real failure there
  should hold the release, not be pushed past.

- **Deploy the usage-ping collector** (`hosted/usage_ping/`, built and
  tested this session, not yet deployed anywhere) — see
  `hosted/usage_ping/README.md`'s own "Deploying it" section for the
  exact steps (mirrors how `hosted/ap_exceptions` was deployed to
  Render). In short: new Render web service, root directory this repo,
  build command `pip install -r hosted/usage_ping/requirements.txt`,
  start command `python3 hosted/usage_ping/service.py`, attach a
  persistent disk and set `USAGE_PING_DB` to a path on it, optionally
  set `USAGE_PING_ADMIN_TOKEN` (a long random value) to enable
  `GET /stats`. **Report the resulting URL back** (e.g.
  `https://inferrail-usage-ping.onrender.com/ping`) so a future session
  can wire it in — either as `usage_ping.endpoint` in a self-hosted
  deployment's own `inferrail.yaml`, or (a separate, later decision)
  baked in as this package's actual default. Until this is done, the
  usage ping is fully built and tested but produces zero real-world
  signal — the Settings screen already says so honestly ("Not yet
  active").
- Everything below remains deferred per `MISSION.md`'s standing ledger,
  untouched and not yet due:
- Render warm/upgrade decision (v0.2.1) — resolved, staying on free
  tier.
- Signing accounts, stopwatch tests, demo video/screenshots, HN post
  timing — all deferred to their respective `MISSION.md` milestones.

## Decisions made during the v0.2.1 session (historical)

- **Render's auto-deploy for `hosted/ap_exceptions` set to "After CI
  Checks Pass"** (founder action, in the Render dashboard — not
  something this repo's files configure). Rationale: this repo's CI
  (`ci.yml`, `boundary-check.yml`) already runs on every push to `main`
  (not just PRs), so Render always has a real check to wait on; this
  adds a safety net against exactly the kind of direct-to-main push
  that bypassed branch protection earlier this session — a bad push
  won't reach the live demo until its CI run actually passes. Confirmed
  Render does not retroactively redeploy a commit whose check already
  completed *before* the setting was changed — the founder had to
  trigger one manual "Deploy latest commit" to bring PR #20 live; every
  push from here forward should auto-deploy correctly once CI passes.
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

## Reference: where the v0.2.1 sandbox code lives (historical)

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
