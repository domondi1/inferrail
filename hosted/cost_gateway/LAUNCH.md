# Cost Gateway -- security review and launch checklist

Companion to `README.md` (architecture, API, env vars) and `PROGRESS.md`
(build status). This file answers two questions for whoever operates the
hosted trial: **what protects visitors and the service today, and how do
we know** (each row names the test that checks it), and **what an
operator still has to do or decide** before calling it launched.

Reviewed 2026-09-24, against `main` after the Phase 5 hardening PRs.

## Security review

| Area | What's in place | Checked by |
|---|---|---|
| **Provider key custody** | Held in process memory only (`keys.py`); never written to disk, never in a receipt, export, log, or API response (responses report booleans only). Discarded on `DELETE .../keys`, on trial end, on expiry purge, and on any restart. A malformed key is rejected without echoing it. | `test_keys_never_appear_in_any_response_body`, `test_submit_keys_rejects_whitespace_key_without_echoing_value`, `test_delete_keys_forgets_key`, `test_purge_loop_runs_and_discards_expired_trial_keys` |
| **Tenant isolation** | Two SQLite/JSONL files per tenant, no shared tables; every data route is scoped to the bearer token's tenant, and a mismatched `tenant_id` in a URL is `403`. | `test_cross_tenant_key_isolation`, `test_cross_tenant_receipts_isolation`, `test_work_and_report_are_isolated_per_tenant`, `test_tenant_id_mismatch_in_path_is_403` |
| **Authentication** | Per-trial bearer token (stored only as a SHA-256 hash); missing, unknown, or expired tokens are `401`. Admin routes use a separate token, compared in constant time, and return `404` when no admin token is configured. | `test_missing_auth_header_is_401`, `test_invalid_api_key_is_401`, `test_expired_trial_is_401`, `test_admin_routes_404_when_no_admin_token_configured`, `test_admin_stats_and_feedback_require_correct_token` |
| **Trial abuse** | Per-address issuance throttle keyed on `True-Client-IP` (set by Cloudflare, not client-controllable; `X-Forwarded-For` is not trusted); global live-trial ceiling; operator kill switch (`COST_GATEWAY_TRIAL_ENABLED=false`). | `test_trial_throttle_ignores_spoofed_x_forwarded_for`, `test_trial_throttle_is_per_real_client`, `test_admin_stats_reports_client_ip_source` |
| **Request abuse** | Per-tenant request rate limit; request body limit counted on bytes actually received (chunked uploads included); whole-request timeout. | `test_rate_limit_enforced`, `test_chunked_body_over_limit_is_rejected`, `test_declared_content_length_over_limit_is_rejected` |
| **Spend** | Real traffic uses the visitor's own key, never an Inferrail key; each trial has a daily budget ($1.00 default) enforced *before* the provider is called. | `test_daily_budget_blocks_demo_requests_when_exceeded`, `test_real_chat_completions_requires_key_returns_400` |
| **Retention** | Demo trials expire after 24h; adding a real key shortens that to at most 4h. A background sweep purges expired trials' data and keys; a failed sweep is logged and the loop keeps running. Visitors can end a trial immediately. | `test_submit_keys_marks_openai_configured_and_tightens_expiry`, `test_end_trial_removes_tenant`, `test_purge_loop_survives_a_failing_sweep` |
| **Logs** | JSON lines built only from a fixed field allow-list -- no headers, bodies, raw URLs, or exception messages -- so keys, tokens, prompts, and feedback text have no path into a log. Every response carries `X-Request-ID`. | `test_logs_never_contain_secrets_or_payloads`, `test_log_event_rejects_fields_outside_allow_list`, `test_every_response_has_request_id_matching_its_log_line` |
| **Feedback privacy** | Feedback (which may include an email) is copied only to a repository the GitHub API confirms is private; there is no default repository. Per-trial and hourly caps limit spam. | `test_feedback_never_filed_to_a_public_repo`, `test_feedback_not_filed_without_an_explicit_repo`, `test_feedback_per_trial_cap`, `test_feedback_github_issues_capped_per_hour` |
| **Memory growth** | Rate-limit and issuance-history tables are pruned as trials are purged and windows pass, so they track current traffic rather than every visitor ever seen. | `test_purged_tenant_leaves_no_rate_limiter_state`, `test_issue_history_pruned_after_window` |
| **Dashboard link** | Each trial's link carries its key only in the URL fragment (never sent to any server or in a `Referer`); the page strips it from the address bar on load and keeps the trial only for that tab (`sessionStorage`). Returned once -- the service stores only a hash of the key. The page warns that anyone with the link can use the trial. | `test_dashboard_link_only_returned_at_creation`; browser check in `TEST_SCRIPT.md` step 5 |
| **Page rendering** | The `/try/` page escapes every value it displays from an API response (model names, work/task ids, model replies), so caller- or model-controlled text is shown as text, never run as markup. | Browser check (work id `<img onerror=…>` rendered as text, no script run) |
| **CORS** | `*` by default -- safe because every route is bearer-token authenticated, with no cookies or ambient credentials for a cross-origin page to ride on. Narrow with `COST_GATEWAY_CORS_ORIGINS` if desired. | `test_cors_preflight_allows_browser_frontend` |

### Accepted limitations (deliberate, documented)

- **Single instance.** Trial, key, and rate-limit state is in process
  memory. Running more than one instance needs a shared store first.
- **Restarts end real-key sessions.** Keys are memory-only by design;
  every redeploy or restart drops them (visitors re-enter the key).
  On hosting without a persistent disk, trial data and the local
  feedback log are also wiped -- the private-repository copy is the
  durable record of feedback.
- **No third-party error tracking.** Errors go to the service's own
  logs only. Adding an external tracker would send data off the box
  and is a separate, explicit decision.
- **Free-tier hosting** sleeps after ~15 minutes idle, so the first
  request after a quiet period can take tens of seconds. The `/try/`
  page says so.

## Launch checklist

Items marked done were completed and verified live during Phase 5.

**Configuration**

- [x] `COST_GATEWAY_ADMIN_TOKEN` set (admin stats/feedback reachable
      only with it).
- [x] Feedback copied to a dedicated **private** repository
      (`COST_GATEWAY_GITHUB_REPO`) with a fine-grained token scoped to
      Issues on that repository only; the startup log line shows
      `"github_feedback_configured": true`.
- [x] Any older GitHub token that could write to a **public**
      repository has been revoked (confirmed 2026-09-24).
- [ ] Calendar reminders set for the GitHub token's and admin token's
      expiry/rotation -- when the GitHub token expires, feedback quietly
      falls back to the local log only (visible in logs as
      `feedback_github_skipped`, `repo_check_failed`, `401`).
- [ ] Optional: `COST_GATEWAY_CORS_ORIGINS` narrowed to the site's own
      origin(s).

**Verification after every deploy**

- [ ] `GET /health` returns `200` with an `X-Request-ID` header.
- [ ] The `startup` log line shows the expected configuration.
- [ ] One trial through `/try/`: demo request works, receipts appear,
      the trial can be ended. For a full check anyone can run, use
      [`TEST_SCRIPT.md`](TEST_SCRIPT.md).
- [ ] `/v1/admin/stats` → `trial_issuance_ip_source` is all
      `true-client-ip` (any `peer` means the trusted header isn't
      arriving).

**Operator decisions (not engineering tasks)**

- [ ] Stay on free-tier hosting (cold starts, no persistent disk) or
      move to a paid instance.
- [ ] Whether to add an external uptime monitor or error tracker (both
      send data to a third party).
- [ ] How often feedback is reviewed.

## Incident runbook

| Situation | Action |
|---|---|
| Abuse or runaway trial creation | Set `COST_GATEWAY_TRIAL_ENABLED=false` and redeploy -- new trials stop, existing ones keep working until they expire. Tighten `COST_GATEWAY_ISSUE_MAX_PER_IP` / `COST_GATEWAY_MAX_LIVE_TENANTS` if needed. |
| A visitor reports an error | Ask for the `X-Request-ID` (also in the body of any 500); search the logs for it. |
| Errors after a deploy | Search logs for `"unhandled_error"` (gives `error_type` and `file:line`); roll back to the previous deploy from the hosting dashboard. |
| Feedback not reaching the private repository | Search logs for `"feedback_github_skipped"` -- `reason` and `http_status` say why (e.g. `repo_check_failed` + `404`: the token can't see the repository). |
| A key may have been exposed | Tell affected visitors to revoke the key at their provider -- only the provider can invalidate it. Restarting the service also drops every key held in memory. |
