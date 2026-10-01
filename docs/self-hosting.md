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

The MCP server is included (`inferrail mcp`). Extras: `inferrail[ap]` for the AP
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

A receipt, from `inferrail demo` (synthetic data, abbreviated):

```json
{
  "receipt_id": "ir_182ef30f1bb44d8db2a4",
  "route": "default",
  "provider": "demo",
  "model": "demo-small",
  "status": "success",
  "prompt_tokens": 812,
  "completion_tokens": 143,
  "pricing": {
    "input_usd_per_million": "0.20",
    "output_usd_per_million": "0.80",
    "source": "DEMO — a made-up round number, not a real provider price",
    "verified_date": "2026-09-30"
  },
  "estimated_cost_usd": "0.000277",
  "attributes": {"customer": "acme", "workflow": "contract-review", "work_id": "work-contract-1"}
}
```

Omitted here: `request_id`, `timestamp`, `total_latency_ms`,
`retry_count`, and the prompt-cache token and price fields (all `null`
here). The full receipt is in
[demo-capture/receipt-priced.txt](assets/demo-capture/receipt-priced.txt). When a model has no price on
file, `pricing` and `estimated_cost_usd` are `null`, and reports count
that request as unknown cost instead of `$0`. Full field list:
[receipts/schema.py](../src/inferrail/receipts/schema.py), or run
`inferrail verify-payload-free`.

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
is contacted when the request's reservation, a conservative estimate,
would exceed what's left of the limit. What's left is the limit minus
recorded spend, minus the reservations of other requests still in
flight. Admission is atomic, so parallel calls sharing a budget (for
example one agent run's `work_id`) can't each spend the same remaining
dollars. When a call can't be priced afterwards (a timeout, or a stream
without usage), its reservation stays counted and is shown on the receipt
as `budget_held_usd`, never as its cost. A request whose actual cost
exceeds its reservation still completes; the overrun is recorded as
`budget_overrun_usd`, and later requests are refused. Setting
`max_tokens` or `max_completion_tokens` keeps reservations close to real
cost. To protect one agent run without creating a budget first, send
`X-Inferrail-Attribute-Work-Id: <run id>` and
`X-Inferrail-Budget-Usd: 0.50` on its requests, or set
`budgets.per_work_default_usd` so every new work_id gets a ceiling (see
[ADR 0022](adr/0022-per-run-budget-declaration.md)). Budgets only cover supported requests routed through this gateway.
They do not see other traffic on your provider account. With a `block`
budget in scope, a model with no known price is refused
(`INFERRAIL_E012`); add a `pricing:` override to use it. See
[ADR 0015](adr/0015-budget-enforcement.md) and
[ADR 0021](adr/0021-atomic-budget-reservations.md).

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
