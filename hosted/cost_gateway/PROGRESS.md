# Cost Gateway — build progress and handoff

**Read this first if you're picking this work up fresh.** Companion to
`README.md` (architecture/API contract) — this file is the fast-moving
"where are we, what's left" status, same role `PROGRESS.md` plays
elsewhere in this project's own convention.

## What this is

A hosted, self-serve, no-account trial of Inferrail: `tryinferrail.com/try/`
lets a visitor start a personal gateway in one click, send zero-key demo
traffic immediately, and optionally add their own OpenAI/Anthropic key
for real, payload-free receipts. Backend: `hosted/cost_gateway/`.
Frontend: `docs/try/index.html` (+ a homepage CTA in `docs/index.html`).

## Status as of 2026-09-23

**Live and working right now** on `main` / `tryinferrail.com`:
- Phase 0 (ADR), Phase 1 (trial provisioning + key handling), Phase 2
  (the `/try/` page + homepage CTA), Phase 3 (work economics/reports/
  transactions, CLI-parity tests) — all merged, all deployed, all
  live-verified.
- The key-entry-form bug fixes (PR #48) and feedback capture +
  founder-facing usage stats (PR #49, includes #48's commit) — **merged
  into `main` and deployed**; `COST_GATEWAY_ADMIN_TOKEN` is set on the
  live Render instance and `/v1/admin/stats`/`/v1/admin/feedback` are
  confirmed working live.
- Backend running on Render free tier at
  `https://inferrail-cost-gateway.onrender.com`.

**Known gotcha, found and fixed the same day:** the free tier has no
persistent disk, so `feedback.jsonl` (and every trial's SQLite files)
get wiped on every redeploy/restart — confirmed directly: feedback
submitted during testing was gone after the next redeploy. Fixed by
the GitHub Issues integration below.

**Also merged, deployed, and live-verified: PR #50** (branch was
`feat/cost-gateway-feedback-github-issues`) — `POST /v1/feedback` now
also files a GitHub Issue (labeled `cost-gateway-feedback`) on
`domondi1/inferrail`, via `COST_GATEWAY_GITHUB_TOKEN` (a fine-grained
PAT, Issues-write-only scope), already set on the live Render instance.
Best-effort: if GitHub is unreachable, the feedback submission still
succeeds via the local write. **Confirmed live** by actually submitting
feedback through the real API and checking the resulting GitHub Issue
existed with the right title/labels/body
(github.com/domondi1/inferrail/issues/51 — a test issue, closed after
verification). The `/v1/admin/feedback`/`/v1/admin/stats` Render-side
view is unaffected and still works the same way.

**As of this update, everything built in this feature is merged,
deployed, and live-verified. No open PRs, no known bugs, nothing
committed-but-unpushed.** Next work is genuinely new scope — see
"Not started yet" below.

## Phase 5 (hardening) -- started 2026-09-24

Split into small PRs, in this order:

- **A. Feedback privacy -- merged (PR #53), deployed, verified live
  2026-09-24.** Feedback used to be filed as an Issue on the *public*
  repo, including a visitor's optional email. Now: no default repo;
  files only to a repo the GitHub API confirms is private; per-trial
  feedback cap (5) and global Issue cap (20/hour). Live check: a real
  submission through the live API produced an Issue in the operator's
  private feedback repo (404 when viewed logged-out), and nothing new
  on the public repo.
- **B. Abuse hardening -- merged (PR #54), deployed, verified live
  2026-09-24.** Trial throttle keys on `True-Client-IP`; body limit
  counts received bytes; limiter tables pruned. Live check: 6 trial
  requests with different spoofed `X-Forwarded-For` values -> 200 x5
  then 429; a spoofed `True-Client-IP` also -> 429 (Cloudflare
  overwrites it); 400 KB upload -> 413; `/v1/admin/stats` showed
  `trial_issuance_ip_source: {"true-client-ip": 7}`, no `peer`.
- **C. Observability -- merged (PR #55), deployed, verified live
  2026-09-24.** Render logs showed the `startup` line with all three
  settings configured, and one allow-listed `request` line per test
  request, each matching its `X-Request-ID`; no keys, tokens, or raw
  URLs. Also showed `/health` logged every ~5s by Render's health
  checker (fixed in D).
- **D. Launch readiness** (branch `chore/cost-gateway-launch-readiness`):
  `LAUNCH.md` -- security review (each protection mapped to its test),
  launch checklist, incident runbook. Plus: FastAPI `lifespan` instead
  of deprecated `on_event`; the purge loop now survives a failed sweep
  (previously one exception would stop all future purges, keeping
  expired trials' keys in memory); successful `/health` checks no
  longer logged.

**After D:** Phase 5 engineering is done. Remaining items are operator
tasks/decisions listed in `LAUNCH.md`'s checklist.

## Phase 2 gaps closed + non-technical test script (2026-09-24)

Branch `feat/cost-gateway-phase2-gaps`. Closes what the original
Phase 2 spec asked for that hadn't shipped:

- **Work/task views + "what does this work cost?"** on `/try/`: tag test
  requests with a Work and Task (standard `X-Inferrail-Attribute-*`
  headers); per-work rows with Succeeded/Failed outcome buttons; a
  summary with known cost and cost per successful outcome; a by-task
  breakdown. Built on the existing `/v1/work` and `/v1/report`.
- **Personal dashboard link** (`dashboard_url`, previously always
  `null`): key in the URL fragment only, stripped on load, returned
  once; reload keeps the trial for that tab.
- **Keep your data**: JSON download of receipts/work/task totals, plus
  the self-host command. Accounts are still Phase 4 -- the page says
  they aren't available, without promising a date.
- **Escaping**: the page previously inserted model names and model
  replies as raw HTML; everything rendered is now escaped.
- **`TEST_SCRIPT.md`**: an 11-step, browser-only test anyone can follow.

Verified with a headless-browser run against a local instance (15
checks: work/task rows, outcome math, HTML-in-work-id shown as text,
download contents with no prompt text, reload, link in a fresh browser,
fragment stripped, ended-trial link, 375px and 1280px with no
horizontal scroll, zero console errors).

## Fix: Anthropic real requests on /try/ (2026-09-24)

Branch `fix/try-page-anthropic-model-discovery`. **Root cause:** the
/try/ page hardcoded `claude-3-5-haiku-20241022` for its real Anthropic
request (and its copy-paste snippet); Anthropic retired that model, so
every real Anthropic request returned `404 model: ...`. The provider
adapter and auth were fine.

**Fix:** new `GET /v1/trial/{tenant_id}/models?provider=anthropic` asks
Anthropic's `GET /v1/models` which models the visitor's key can use --
server-side, with the in-memory key, returning model ids only. The
lightest family (Haiku, then Sonnet, then Opus; unknown families rank
with Sonnet) and newest release within it is selected
(`select_anthropic_test_model`); no dated model id is hardcoded
anywhere. The page discovers once a key is added, uses the selected
model for the real request and the snippet, shows which model was used,
and shows distinct messages for a rejected key (400
`provider_key_rejected`), a failed lookup (502 `model_discovery_failed`),
or no usable model. `models` is returned whole, so a model picker can be
added later by just setting the page's `models.anthropic.selected`.

## The recurring blocker every pass hits: no push access

This session's git credentials cannot push to `domondi1/inferrail`
(403). Every branch above exists only as a local commit until the
founder runs `git push -u origin <branch>` themselves (then `gh pr
create`/`gh pr merge`, or update the existing PR). This has happened on
every single PR in this feature's history — plan for it, don't be
surprised by it. **One real incident this caused:** PR #45 was merged
before its last two commits finished pushing, leaving the live site
briefly pointed at a placeholder URL — fixed by PR #46. Lesson: confirm
the push landed (`git log origin/<branch> --oneline -1`) before merging,
not just after asking for a push.

## Architecture notes worth knowing before touching this again

- Every hosted `/v1/...` route is reused/thin-wrapped over the *same*
  code the self-hosted CLI uses (`InferenceEngine`, `Router`,
  `OpenAIProvider`, `inferrail.work.builder`, etc.) — see README's
  "Parity with the CLI" section. Don't reimplement wire-format or
  aggregation logic here; import it.
- Provider keys: in-memory only, per-tenant, never persisted — see
  `keys.py`'s threat-model docstring before touching key handling at
  all, and restate the threat model in any PR description that does.
- `docs/try/index.html`'s `DEFAULT_BASE_URL` constant is the one thing
  that must be manually updated if the Render service is ever
  redeployed under a different URL/name.
- Test-injection seam for real-provider proxying:
  `service.py`'s `openai_client_factory`/`anthropic_client_factory` —
  monkeypatch these in tests to inject an `httpx.MockTransport`, same
  pattern `tests/unit/test_providers.py` already uses.
- Render free tier = no persistent disk (deliberately not needed — see
  README) and cold starts after ~15 min idle; the trial page has honest
  UI copy for this, don't remove it if the tier ever changes back to
  free after being upgraded.
- This session hit the trial-issuance rate limit (5/IP/hour) from its
  own repeated testing more than once. Don't be alarmed if a fresh
  session's live-verification curl gets a 429 from a previous session's
  testing — either wait, or spin up a local instance with
  `COST_GATEWAY_ISSUE_MAX_PER_IP` raised for testing purposes.

## Not started yet (original Phase 4 scope)

- **Accounts**: no way to "claim" a trial and persist it past its TTL.
  No auth beyond the bearer-token-per-trial model that already exists.
- **Email digest** (opt-in, work-level cost summary) — not built.
- **Transparent opt-in update/feature-flag channel** — not built.
- **Data export/deletion beyond what already exists**: a visitor can
  already `GET /v1/receipts` and `DELETE /v1/trial/{id}` themselves;
  nothing beyond that (e.g. a one-click "download everything" bundle)
  exists yet.
- **A persistent (non-process-local) usage counter** — flagged in the
  README's new "Feedback and usage visibility" section as the natural
  next step once the in-memory `trials_issued_total` stops being enough
  (it resets on every restart/redeploy).
- **A hosted dashboard reusing the local React app** (the ADR's original
  Phase 2 aspiration) was descoped in favor of the plain-HTML `/try/`
  page that shipped instead — documented as a deliberate difference in
  README's "Parity with the CLI" section, not an oversight.

## Suggested next session's first move

1. Confirm what's actually on `main`/live by the time you read this
   (`git log origin/main --oneline -10`, plus a real `curl .../health`)
   — don't trust this file blindly; re-verify rather than assume
   nothing's changed since it was written.
2. If PR D (launch readiness) is merged, Phase 5 engineering is done:
   check the operator items in `LAUNCH.md`'s checklist with the founder
   rather than assuming them done.
3. Next direction is a founder call — ask, don't assume: **Phase 4**
   (accounts to persist a trial past its TTL, opt-in email digest,
   transparent update channel, richer data export -- see "Not started
   yet" above) or something else entirely.
4. Keep the same verification discipline every pass in this feature's
   history has used: curl the real live endpoint, check the logs, and
   drive the real page in a real headless browser — don't just trust
   that code merged means code works.
