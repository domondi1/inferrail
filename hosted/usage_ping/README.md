# Inferrail Usage Ping Collector

The reference receiver for the opt-in, anonymous usage ping
(`docs/adr/0019-opt-in-usage-ping.md`, `docs/privacy/usage-ping.md`).
Its own process, its own SQLite file, no shared code path with
`hosted/ap_exceptions` or `hosted/a2a_economic_authority` — and, unlike
those two, **zero dependency on the `inferrail` package**: the payload
this service accepts is small, fixed, and self-contained.

Accepts one event type per request, at `POST /ping`, matching
`inferrail.usage_ping.payload.build_payload` exactly:

```json
{
  "install_id": "a1b2c3d4e5f6...",
  "event": "first_run",
  "os": "macos",
  "inferrail_version": "0.4.1",
  "ts": "2026-09-15T05:50:00.144354+00:00"
}
```

Anything outside that exact shape is rejected with a `422` — this
service enforces the privacy boundary server-side too, not just by
trusting the client.

## What it deliberately does not do

- No authentication on `POST /ping` — the payload is harmless and
  anonymous, so a key would only add friction, not privacy.
- **Never logs or persists the connecting IP address**, anywhere.
- Never returns per-install detail over HTTP — `GET /stats` (disabled
  entirely unless `USAGE_PING_ADMIN_TOKEN` is set) is aggregate counts
  only ("how many," never "who").
- Never executes anything on behalf of a caller — it only ever writes
  one row per accepted event.

## Running it locally

```
python3 hosted/usage_ping/service.py 8600
```

then, from another terminal:

```
curl -s -X POST http://127.0.0.1:8600/ping \
  -H "Content-Type: application/json" \
  -d '{"install_id":"test","event":"first_run","os":"linux","inferrail_version":"0.4.1","ts":"2026-09-15T00:00:00Z"}'

curl -s http://127.0.0.1:8600/health
```

## Deploying it (Render, or any host that runs a long-lived Python process)

1. Build command: `pip install -r hosted/usage_ping/requirements.txt`
   (no `pip install -e .` needed — this service imports nothing from the
   main package).
2. Start command: `python3 hosted/usage_ping/service.py` (no CLI args —
   binds `0.0.0.0`, reads `$PORT`).
3. Attach a persistent disk and set `USAGE_PING_DB` to a path on it
   (default: `./usage-ping.sqlite3`, which is ephemeral on most
   platforms without one).
4. Optional: set `USAGE_PING_ADMIN_TOKEN` to a long random value to
   enable `GET /stats` (`Authorization: Bearer <token>`) — leave unset
   to disable it entirely (a bare `404`).
5. Health check path: `/health`.
6. Once deployed, give the resulting URL (e.g.
   `https://inferrail-usage-ping.onrender.com/ping`) to whoever is
   setting `usage_ping.endpoint` in the main package's `inferrail.yaml`
   default, or to `PROGRESS.md`'s "HUMAN ACTION NEEDED" entry that's
   waiting on it.

## Environment variables

- `USAGE_PING_DB` — SQLite file path (default `./usage-ping.sqlite3`).
- `USAGE_PING_ENABLED` — kill switch; `false` makes `POST /ping` return
  `200` without storing anything, so a caller never sees an error even
  while collection is turned off server-side (default `true`).
- `USAGE_PING_ADMIN_TOKEN` — bearer token required for `GET /stats`;
  unset disables that route entirely.
- `USAGE_PING_MAX_REQUEST_BODY_BYTES` — request-size cap (default 4096).
- `USAGE_PING_RATE_LIMIT_MAX_REQUESTS` /
  `USAGE_PING_RATE_LIMIT_WINDOW_SECONDS` — per-IP fixed-window rate
  limit (default 30 requests / 60s; the limiter key itself is kept only
  in memory, never persisted).

## Rollback

Additive-only schema (`CREATE TABLE IF NOT EXISTS`), no migrations yet.
Redeploy the previous commit against the same `USAGE_PING_DB` disk;
verify with `GET /health`.
