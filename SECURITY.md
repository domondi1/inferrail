# Security Policy

Inferrail is pre-1.0 (`0.1.x`). There is no long-term support commitment
for any specific version yet — please always run the latest `main`.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security or privacy
vulnerabilities (credential handling, auth bypass, telemetry that could
leak prompt/response content, etc.).

Instead, email **danieldocokumu@gmail.com** with a description and, if
possible, steps to reproduce. We'll acknowledge reports as soon as we can
and let you know once a fix is available.

## Scope and known posture

A few things worth knowing if you're evaluating Inferrail's security model
rather than reporting a specific bug:

- **The gateway is a local development tool by default.** It binds to
  `127.0.0.1` and does not authenticate callers unless you set
  `INFERRAIL_GATEWAY_TOKEN`. If you expose it beyond your own machine
  without setting that, anyone who can reach it can consume your
  configured provider credentials. See README.md's "Configuration" section.
- **`INFERRAIL_GATEWAY_TOKEN` is a single shared secret**, not a user/auth
  system — sufficient to keep strangers off your gateway, not for
  multi-tenant access control.
- **No prompt or response content is persisted by default**, structurally
  (the telemetry schema has no field for it), and no telemetry is ever
  sent to any Inferrail-operated service — there isn't one yet. See
  `docs/adr/0003-no-payload-persistence-by-default.md`.
- Provider API keys are read from environment variables at process start
  and are never written to `inferrail.yaml`, logs, or telemetry.
