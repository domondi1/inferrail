# The usage/presence beacon

This page exists so you never have to take Inferrail's word for what the
usage beacon sends. Everything below is also checkable yourself, on your
own machine, with:

```
inferrail telemetry preview
```

which prints the exact payload for every event this install would ever
send, built from your real install id, without sending anything.

## On by default, but inert until an endpoint is configured

As of this release, the usage beacon is **on by default** (a
deliberate reversal of this project's original "opt-in, off by default"
design — see
[ADR-0020](../adr/0020-quickstart-both-sdks-and-payload-free-verification.md),
which also records why).

It is still **inert with no collection endpoint configured**, regardless
of the on/off toggle. Inferrail does not ship with a default endpoint
baked in; an operator has to explicitly set `usage_ping.endpoint` in
`inferrail.yaml` before anything can ever be sent, from anyone's install.
If you see "Not yet active" in the Settings screen or `inferrail
telemetry status`, that's why — the overwhelming majority of installs,
which never configure an endpoint, send nothing at all, ever.

## Turning it off

Any one of these, and no event is ever sent again:

```
inferrail telemetry disable
```

or uncheck the toggle in the dashboard's Settings screen, or:

- `INFERRAIL_TELEMETRY=0` (any `inferrail` command)
- `--no-telemetry` (on `inferrail serve`)
- `DO_NOT_TRACK=1` (the [consoledonottrack.com](https://consoledonottrack.com/) convention)

It's also **off automatically** under common CI environment variables and
under this project's own test suite — no configuration needed for either.

## Exactly what is sent

One small JSON object per event, over HTTPS, to the endpoint configured
in `inferrail.yaml`:

```json
{
  "install_id": "a1b2c3d4e5f6...",
  "event": "install",
  "version": "0.4.1",
  "os": "macos",
  "python_version": "3.12"
}
```

- **`install_id`** — a random identifier generated on this machine the
  first time it's needed (`uuid4`), stored locally. It is not derived
  from any hardware identifier, MAC address, disk serial, hostname, or
  anything else that could identify this specific machine or person —
  it exists only so a receiving collector can tell two events came from
  the same install, or two different ones.
- **`event`** — one of exactly four lifecycle milestones:
  - `install` — the first time `inferrail serve` ever runs on this
    machine with the beacon enabled. Sent **at most once, ever**.
  - `serve_start` — every time `inferrail serve` starts (quickstart,
    `--app-mode`, or a plain config-based deployment — all of them).
  - `first_receipt` — the first receipt ever recorded on this install
    (fires even on a failed/blocked request, since a receipt is still
    produced). Sent **at most once, ever**.
  - `heartbeat` — sent **at most once per 24 hours** while a server
    process keeps running, so a long-running, low-traffic deployment
    still shows up as active.
- **`version`** — the installed `inferrail` package version.
- **`os`** — `linux`, `macos`, or `windows`.
- **`python_version`** — major.minor only (e.g. `"3.12"`), never a full
  patch/build string.

There is no timestamp field in the payload itself — the collector stamps
`seen_at`/`last_seen_at` on arrival and never trusts a client clock.

## What is never sent

Never, under any circumstance, in this or any future version of this
payload without a new, separately-documented decision:

- Any prompt, response, or other request/response content.
- Model names, providers, costs, token counts.
- `work_id`, project names, customer names, or any other business
  attribution.
- Anything about your actual traffic volume or patterns.
- Your IP address is not included in the payload (HTTP itself always
  carries a connecting IP at the network layer — see "The receiver"
  below for what the reference receiver implementation does with it:
  nothing; it isn't logged or stored).

## If the ping fails, or you're offline

It fails silently. A network error, a timeout, an unreachable endpoint,
or being fully offline never blocks, slows, or errors the gateway or any
request going through it — the send happens on a background thread the
gateway never waits on, and any failure there is swallowed. Startup
itself never waits on the network either, even with the beacon enabled
and an endpoint configured.

## The receiver

The reference collector (`hosted/usage_ping/` in this repository) is a
small, open-source FastAPI service: one endpoint, a per-IP rate limit, a
request-size cap, a kill switch, and it does not log or persist the
connecting IP address. It stores two small tables — one row per install
(with a `reached_first_receipt_at` timestamp, set once, so the operator
can tell installs apart from *activated* installs) and an append-only
log of individual events. You can read its full source, or run your own
instance and point `usage_ping.endpoint` at it instead of Inferrail's.
`scripts/owner_stats.py` reads that same database directly for a human
summary (total installs, activation rate, active in the last 7/30 days,
new installs per week) — see its own docstring.

## Source

Everything above is implemented in `src/inferrail/usage_ping/` — see
[ADR-0020](../adr/0020-quickstart-both-sdks-and-payload-free-verification.md)
for the full design decision and its reasoning, and
[ADR-0019](../adr/0019-opt-in-usage-ping.md) for the original design this
one builds on (superseded only on the on/off default, nothing else).
