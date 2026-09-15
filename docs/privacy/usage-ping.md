# The opt-in usage ping

This page exists so you never have to take Inferrail's word for what the
usage ping sends. Everything below is also checkable yourself, on your
own machine, with:

```
inferrail telemetry preview
```

which prints the exact payload for every event this install would ever
send, built from your real install id, without sending anything.

## Off by default

The usage ping is **off unless you explicitly turn it on** — in the
dashboard's Settings screen, or with `inferrail telemetry enable`. No
event is ever sent before you do.

It is also **inert with no collection endpoint configured**, regardless
of the toggle. Inferrail does not ship with a default endpoint baked in;
an operator has to explicitly set `usage_ping.endpoint` in
`inferrail.yaml` before anything can ever be sent, from anyone's
install. If you see "Not yet active" in the Settings screen, that's why.

## Exactly what is sent

One small JSON object per event, over HTTPS, to the endpoint configured
in `inferrail.yaml`:

```json
{
  "install_id": "a1b2c3d4e5f6...",
  "event": "first_run",
  "os": "macos",
  "inferrail_version": "0.4.1",
  "ts": "2026-09-15T05:50:00.144354+00:00"
}
```

- **`install_id`** — a random identifier generated on this machine the
  first time it's needed (`uuid4`), stored locally. It is not derived
  from any hardware identifier, MAC address, disk serial, hostname, or
  anything else that could identify this specific machine or person —
  it exists only so a receiving collector can tell two events came from
  the same install, or two different ones.
- **`event`** — one of exactly four lifecycle milestones, each sent **at
  most once per install, ever**:
  - `first_run` — `inferrail serve --app-mode` started for the first
    time with the ping enabled.
  - `tool_connected` — the first real client request the gateway
    completed successfully (the OpenAI SDK, Claude Code, curl, whatever
    you pointed at it).
  - `first_receipt` — the first receipt ever recorded (fires even on a
    failed/blocked request, since a receipt is still produced).
  - `budget_created` — the first budget you created via the dashboard.
- **`os`** — `macos`, `linux`, or `windows`.
- **`inferrail_version`** — the installed package version.
- **`ts`** — when the event happened, in UTC.

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
gateway never waits on, and any failure there is swallowed.

## Turning it off

```
inferrail telemetry disable
```

or uncheck the toggle in the dashboard's Settings screen. Once-sent
lifecycle events aren't "unsent," but no further event will ever be sent
until you turn it back on.

## The receiver

The reference collector (`hosted/usage_ping/` in this repository) is a
small, open-source FastAPI service: one endpoint, a per-IP rate limit, a
request-size cap, a kill switch, and it does not log or persist the
connecting IP address. You can read its full source, or run your own
instance and point `usage_ping.endpoint` at it instead of Inferrail's.

## Source

Everything above is implemented in `src/inferrail/usage_ping/` — see
`docs/adr/0019-opt-in-usage-ping.md` for the full design decision and
its reasoning.
