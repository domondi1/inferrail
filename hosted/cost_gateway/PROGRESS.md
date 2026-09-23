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

**Known live gotcha, found right after deploying #49:** the free tier
has no persistent disk, so `feedback.jsonl` (and every trial's SQLite
files) get wiped on every redeploy/restart — confirmed directly:
feedback submitted during testing was gone after the next redeploy.
This is why the GitHub Issues integration below exists.

**Committed locally, not yet pushed (same recurring push-permission gap
— see below), branch `feat/cost-gateway-feedback-github-issues`:**

- Adds a **second, durable destination** for feedback: `POST
  /v1/feedback` now also files a GitHub Issue (labeled
  `cost-gateway-feedback`) on `COST_GATEWAY_GITHUB_REPO`
  (`domondi1/inferrail`), via a new `COST_GATEWAY_GITHUB_TOKEN` secret
  (fine-grained PAT, Issues-only scope). Best-effort: if GitHub is
  unreachable or the token's wrong, the feedback submission still
  succeeds and still gets the local (ephemeral) write — GitHub never
  blocks the local one. The existing `/v1/admin/feedback` Render-side
  view is unchanged and still works for whatever's arrived since the
  last redeploy.
- 3 new tests (41 total), ruff/mypy/boundary-check clean.
- **Not yet deployed or verified live** — needs push, PR, merge,
  `COST_GATEWAY_GITHUB_TOKEN` set on Render (a fine-grained PAT scoped
  to Issues-write on `domondi1/inferrail` only), redeploy, then a real
  end-to-end check (submit feedback, confirm a real Issue appears).

**To get these live:** push the branch(es), open PR(s) (or update the
existing ones), get CI green, merge, then **set
`COST_GATEWAY_ADMIN_TOKEN`** as a new Render secret and redeploy (the
admin routes stay 404'd/harmless until you do). Generate it yourself,
e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` —
never share it with an assistant session.

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

## Not started yet (original Phase 4 / Phase 5 scope)

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
- **Phase 5 (hardening/observability/launch checklist)**: no
  structured-logging/metrics/error-tracking pass has been done on this
  service specifically; no formal security review beyond what's already
  documented inline; no cost-control review beyond the existing
  per-tenant daily budget default ($1.00).
- **A hosted dashboard reusing the local React app** (the ADR's original
  Phase 2 aspiration) was descoped in favor of the plain-HTML `/try/`
  page that shipped instead — documented as a deliberate difference in
  README's "Parity with the CLI" section, not an oversight.

## Suggested next session's first move

1. Confirm what actually got merged since this was written (`git log
   origin/main --oneline -10`) — don't assume the two branches above
   are still unmerged by the time you read this.
2. If they're merged: set `COST_GATEWAY_ADMIN_TOKEN`, redeploy, verify
   `/v1/admin/stats` and the "Report an issue" form live, same
   discipline every prior pass in this feature's history used (curl the
   real endpoint, then drive the real page in a real headless browser —
   don't just trust the code).
3. Then either continue toward Phase 4 (accounts) if the founder wants
   to keep building, or treat what's live now as a real v1 and shift to
   Phase 5 hardening/launch-readiness — that's a founder call, not an
   engineering one; ask rather than assume.
