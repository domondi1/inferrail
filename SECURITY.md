# Security Policy

Inferrail is a developer preview (alpha, pre-1.0). There is no long-term
support commitment for any specific version. Please use the latest PyPI
release or `main`.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security or privacy
vulnerabilities (credential handling, auth bypass, request content
reaching a stored record or log, and similar).

Instead, email **danieldocokumu@gmail.com** with a description and, if
possible, steps to reproduce. We'll acknowledge reports as soon as we can
and let you know once a fix is available.

## Scope and known posture

This section describes what the code does today. It is not a security
audit or certification.

### Self-hosted gateway (`inferrail serve`)

- **Local by default.** The gateway binds to `127.0.0.1:8000` and does
  not authenticate callers unless you set `INFERRAIL_GATEWAY_TOKEN`. If
  you expose it beyond your machine without that, anyone who can reach
  it can spend your configured provider credentials.
- **`INFERRAIL_GATEWAY_TOKEN` is one shared secret**, not a user or role
  system.
- **Provider keys** are read from the environment variable named in
  config (`api_key_env`). Inferrail does not write them to
  `inferrail.yaml`, receipts, or telemetry. The gateway sends the key to
  the provider you configure, as any client would.
- **Request and response content** passes through the gateway process in
  memory and goes to your configured provider, which applies its own
  policies. Receipts and telemetry events are built without message
  bodies, and operator-facing error logs use a category-only summary
  rather than upstream error text. See
  [`receipts/builder.py`](src/inferrail/receipts/builder.py),
  [`telemetry/events.py`](src/inferrail/telemetry/events.py), and the
  canary tests in
  [`test_gateway_receipts.py`](tests/unit/test_gateway_receipts.py),
  [`test_gateway.py`](tests/unit/test_gateway.py), and
  [`test_gateway_anthropic.py`](tests/unit/test_gateway_anthropic.py).
- **Attribution values are stored as sent.** `X-Inferrail-Attribute-*`
  headers become receipt `attributes` verbatim. Keep secrets and message
  content out of them.
- **Usage beacon.** An anonymous lifecycle beacon exists. Its `enabled`
  flag defaults to on, but the package ships with no collection
  endpoint, so nothing is sent unless `usage_ping.endpoint` is
  configured. It never carries prompts, responses, models, costs, or
  attribution. Turn it off with `inferrail telemetry disable`,
  `INFERRAIL_TELEMETRY=0`, `--no-telemetry`, or `DO_NOT_TRACK=1`. See
  [docs/privacy/usage-ping.md](docs/privacy/usage-ping.md).
- `inferrail verify-payload-free` lists the receipt fields and checks
  their names. It does not inspect stored values or logs.

### Hosted services

Inferrail also operates separate hosted services from the `hosted/`
directory, including a hosted cost-gateway trial. They are not part of
the `inferrail` package, and the self-hosted gateway never depends on
them.

In the hosted cost-gateway trial, if you submit a real provider key, the
hosted process receives that key and handles your traffic. Per
[`hosted/cost_gateway/keys.py`](hosted/cost_gateway/keys.py) and
[`trial.py`](hosted/cost_gateway/trial.py), the key is held in process
memory only and is not written to disk. A trial expires after at most 24
hours, and at most 4 hours after a real key is added. Expired trials
are purged (periodically, on lookup, or on explicit delete), which
deletes the tenant's files and its in-memory key. A restart also drops
every in-memory key. If you do not want any hosted
process to hold your key, self-host instead. Details:
[hosted/cost_gateway/README.md](hosted/cost_gateway/README.md).
