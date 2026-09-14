# Changelog

All notable changes to Inferrail are recorded here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versions
correspond to the milestones in `MISSION.md`, not necessarily to a new
PyPI release (the hosted service and website ship independently of the
`inferrail` package).

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
