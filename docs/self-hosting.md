# Running the gateway yourself

Configuration, storage, budgets, and operating limits for
`inferrail serve`. For request integration, see
[integrations.md](integrations.md). For exact scope, see
[PRODUCT.md](PRODUCT.md).

## Install

Inferrail needs Python 3.11 or newer.

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install inferrail
inferrail --help
```

If your shell says `inferrail: command not found`, the virtual
environment is not active, or `pip` installed into a different Python
than the one on your `PATH`. Activate the environment, or run
`python -m pip show inferrail` to see where it went.

Extras: `inferrail[mcp]` for the MCP server, `inferrail[ap]` for the AP
retry adapter's OpenAI SDK path.

## Configuration

Quickstart (`inferrail serve --quickstart`) needs no file. For an
explicit setup:

```bash
cp inferrail.example.yaml inferrail.yaml
inferrail config check    # validate without starting a server
inferrail serve
```

`inferrail.yaml` holds the **name** of the environment variable for each
provider key (`api_key_env`), never the key itself. Full shape
(providers, routes, telemetry, receipts, pricing overrides, budgets,
usage beacon): [inferrail.example.yaml](../inferrail.example.yaml) and
[config.schema.json](../config.schema.json).

## Security defaults

- The gateway binds to `127.0.0.1:8000` and does **not** authenticate
  callers by default. Before exposing it to other machines, set
  `INFERRAIL_GATEWAY_TOKEN`; callers must then send
  `Authorization: Bearer <token>`. It is one shared secret, not a user
  system.
- Anyone who can reach an unauthenticated gateway can spend your
  provider credentials.
- See [SECURITY.md](../SECURITY.md) for the full posture and how to
  report a vulnerability.

## Where records go

| Record | Default location | Contains |
|---|---|---|
| Receipts | `./inferrail-receipts.jsonl` (relative to the gateway's working directory) | One line per supported request: tokens, price snapshot, cost, status, timing, attributes |
| Telemetry events | stdout (console sink) | Operational metadata: status, error category, latency, tokens |
| Work outcomes | `./inferrail-work-outcomes.jsonl` | `work_id`, your outcome label, timestamp |

Set `receipts.sink: sqlite` for an indexed WAL-mode SQLite store
([ADR 0013](adr/0013-sqlite-receipts-store.md)), or `none` to disable
receipts.

**Single host.** Every process that should appear in one report must
write to one file on one filesystem. Concurrent writers on the same host
are safe (JSONL uses one atomic `O_APPEND` write per receipt; SQLite uses
WAL plus a busy timeout). Nothing merges ledgers across machines.
`inferrail report` reads the whole file into memory, and there is no
retention or rotation, so rotate large files yourself.

## Budgets

Opt-in, and requires `receipts.sink: sqlite`. Budgets can be `global`,
per `project`, or per `work_id`, over a `per_work`, `daily`, or
`monthly` window, in `warn` or `block` mode:

```bash
inferrail serve --quickstart --daily-budget-usd 5
inferrail budget set --help
```

A `block` budget rejects a request with HTTP 402 **before** any provider
is contacted, using a conservative upper-bound estimate. Budgets only
cover supported requests routed through this gateway. They do not see
other traffic on your provider account. A request for a model with no
known price has no estimate, so it is **not** checked against the limit
and is allowed through. See
[ADR 0015](adr/0015-budget-enforcement.md).

## Local dashboard and control API

```bash
inferrail serve --quickstart --app-mode
```

`--app-mode` moves receipts and budgets under the OS app-data directory,
mounts a token-protected local control API (`/v1/local/*`), and serves
the local dashboard when a build is present. The PyPI wheel includes the
dashboard build; a source checkout without Node simply runs without it.
The startup banner prints a URL with the local token embedded. See
[ADR 0016](adr/0016-local-control-api.md) and
[ADR 0017](adr/0017-dashboard-in-app-directory.md).

## Usage beacon

An anonymous lifecycle beacon exists (`install`, `serve_start`,
`first_receipt`, `heartbeat`). Its `enabled` flag defaults to on, but no
collection endpoint ships with the package, so **nothing is sent unless
`usage_ping.endpoint` is configured**. It never carries prompts,
responses, models, costs, or attribution.

```bash
inferrail telemetry status    # is it on, and is an endpoint set?
inferrail telemetry preview   # the exact payload, without sending
inferrail telemetry disable   # or INFERRAIL_TELEMETRY=0, --no-telemetry, DO_NOT_TRACK=1
```

Full disclosure: [privacy/usage-ping.md](privacy/usage-ping.md).

## Diagnostics

- `inferrail doctor`: port, pricing-catalog freshness, provider
  reachability.
- `inferrail pricing update`: reports the built-in catalog's age. It
  never fetches prices over the network.

## Development

```bash
git clone https://github.com/domondi1/inferrail.git && cd inferrail
pip install -e ".[dev,mcp,ap]"
ruff check . && mypy && pytest
```

`pytest` needs no API key or network access. See
[CONTRIBUTING.md](../CONTRIBUTING.md).
