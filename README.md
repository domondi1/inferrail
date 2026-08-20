# Inferrail

<!-- mcp-name: io.github.domondi1/inferrail -->

[![CI](https://github.com/domondi1/inferrail/actions/workflows/ci.yml/badge.svg)](https://github.com/domondi1/inferrail/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Status: Developer Preview](https://img.shields.io/badge/status-developer%20preview-orange.svg)](docs/PRODUCT.md)

Inferrail is an open-source, self-hosted, OpenAI-compatible gateway that
measures the cost of every LLM request and attributes it to the customer,
workflow, or feature that drove it — without ever storing the prompt or
response. Point your app's OpenAI client at Inferrail instead of directly
at your provider, and every call produces a payload-free economic receipt:
verified pricing, measured token usage, and whatever business attribution
you attach.

**The problem:** if your app calls an LLM provider on behalf of different
customers, workflows, or features, you probably know your total bill —
but not which one is driving it. Finding that out today usually means
adding ad hoc logging or a hosted observability product. Inferrail sits
in the request path, measures each request's real token usage, estimates
its cost against a verified price, and lets you attribute it to whatever
business context you choose — without ever storing the prompt or response
that produced it.

**Developer Preview · v0.1.0**

Inferrail works today, but its CLI, configuration, and receipt schemas may
evolve before 1.0.

Longer compatibility guidance may remain in
[docs/PRODUCT.md](docs/PRODUCT.md).

## Quickstart

Requires Python 3.11+. Installs, starts a gateway with a freshly
generated auth token (no placeholder to fill in, no `OPENAI_API_KEY`
needed for this step), verifies it's up via `/health`, then stops it.
Idempotent — safe to run twice.

```bash
git clone https://github.com/domondi1/inferrail.git && cd inferrail
pip install -e .

export INFERRAIL_GATEWAY_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
inferrail serve --quickstart > /tmp/inferrail-quickstart.log 2>&1 &
INFERRAIL_PID=$!
for i in $(seq 1 20); do
  curl -sf http://127.0.0.1:8000/health > /dev/null 2>&1 && break
  sleep 0.25
done
if curl -sf http://127.0.0.1:8000/health; then
  echo "Inferrail is up."
  kill "$INFERRAIL_PID"
else
  echo "Inferrail failed to start — see /tmp/inferrail-quickstart.log" >&2
  kill "$INFERRAIL_PID" 2>/dev/null
  exit 1
fi
```

That proves the install and the gateway process start correctly — it
doesn't yet show Inferrail's actual point (cost attribution), since that
needs either a real provider key or the offline demo immediately below.

## Try it interactively

### See the magic moment (no key, no network)

```bash
inferrail demo
```

Runs six canned requests through Inferrail's **real** `InferenceEngine` →
`InferenceReceipt` → `inferrail report` pipeline, using a fake in-memory
provider instead of a network call — no key, no signup, no cost, done in
under a second. Every price and response it uses is clearly labeled
**DEMO** data, not real provider billing.

### Try it for real (one request, an estimated cost)

```bash
export OPENAI_API_KEY=<your-openai-api-key>
inferrail try "Reply with one word: ready" --customer acme
```

No `inferrail.yaml`, no server, no curl, no HTTP headers. `inferrail try`
sends one real request through the exact same `InferenceEngine`
`inferrail serve` uses (OpenAI, `gpt-4o-mini` by default) and prints the
response plus its economic receipt:

```
ready

Receipt ir_670b20135cfe4bcb8f6f
  Provider          openai
  Model             gpt-4o-mini
  Input tokens      12
  Output tokens     1
  Cost              $0.000002
  Customer          acme
  Prompt stored     no
  Response stored   no

Saved to ./inferrail-receipts.jsonl

Next:
  inferrail report --by customer
```

Attach more business context generically with repeatable `-a KEY=VALUE`
(`--customer`/`--workflow` are shorthand for `-a customer=...`/
`-a workflow=...`). No `OPENAI_API_KEY`? `inferrail try` tells you to run
`inferrail demo` instead, rather than failing with a stack trace.

### Run it as a gateway

```bash
inferrail serve --quickstart
```

Same defaults as `inferrail try` (OpenAI, `gpt-4o-mini`, receipts at
`./inferrail-receipts.jsonl`), no `inferrail.yaml` required — every
`/v1/chat/completions` request now produces the same kind of receipt shown
above. For a real, checked-in deployment instead of quickstart defaults,
see [Configure](#configure) below.

### See what it's costing you

```bash
inferrail report --by customer
```

```
CUSTOMER        REQUESTS  INPUT TOKENS  OUTPUT TOKENS  COST (USD)  UNKNOWN COST
acme            5         3368          764            $0.000964
globex          2         1684          382            $0.000482
(unattributed)  1         842           191            $0.000241
--------------  --------  ------------  -------------  ----------  ------------
TOTAL           8         5894          1337           $0.001687
```

`--by` also accepts `provider`, `model`, `route`, or any other attribute
name you've sent. Works immediately after `inferrail try`/`inferrail serve
--quickstart` — no `--config` needed.

## What it stores — and doesn't

Every request produces a local, payload-free `InferenceReceipt`: provider,
model, measured token usage, a `Decimal` cost with its pricing source and
date, and whatever business tags you attached. **There is no field
capable of holding a prompt or response** — a schema-level guarantee,
enforced by a test, not a setting that could be misconfigured on. Nothing
is ever sent to any Inferrail-operated service — there isn't one yet.

The one deliberate exception: attribute values you explicitly attach
(`--customer`, `X-Inferrail-Attribute-*`) are persisted verbatim, since
they're metadata you declared, not content extracted from the
conversation — don't put secrets in them.

Full guarantee, what *does* leave the machine (your provider still gets
the real prompt), and a 60-second way to verify this yourself:
[Privacy](#privacy).

## Use it with your application

With `inferrail serve` (or `--quickstart`) running:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Inferrail-Attribute-Customer: acme" \
  -d '{
    "model": "default",
    "messages": [{"role": "user", "content": "Say hello in five words."}]
  }'
```

`"model": "default"` selects the `default` route in `inferrail.yaml` (or
the quickstart config) — not a provider's model id directly. You aren't
limited to route names, though: the quickstart config (and any
`inferrail.yaml` with `default_provider` set) forwards any other `model`
value straight to that provider unchanged — `"model": "gpt-5.6-sol"` works
with no config change, including for a model that didn't exist when this
version of Inferrail shipped. See
[docs/adr/0007-model-passthrough-routing.md](docs/adr/0007-model-passthrough-routing.md).
The `X-Inferrail-Attribute-*` header is optional business attribution; drop it
if you don't need it — it's never forwarded to the upstream provider. The
response includes the standard OpenAI `choices`/`usage` fields plus a
non-standard `inferrail` block (route, provider, latency, retries) that
standard OpenAI clients can ignore. See
[examples/basic_chat_request.py](examples/basic_chat_request.py) for a
minimal Python client.

### With an existing framework

Any OpenAI-compatible client works unmodified against Inferrail — point
its `base_url` at `http://127.0.0.1:8000/v1` (note the `/v1`) and use an
Inferrail route name (e.g. `"default"`) as the model. No Inferrail SDK or
adapter exists or is needed. Verified against a real client instance of
each (not just read from the framework's docs):

```python
# LangChain
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="not-needed",  # or your INFERRAIL_GATEWAY_TOKEN if auth is enabled
    model="default",       # an Inferrail route name, not a provider model id
)
```

```python
# LlamaIndex
from llama_index.llms.openai_like import OpenAILike

llm = OpenAILike(
    model="default",
    api_base="http://127.0.0.1:8000/v1",
    api_key="not-needed",
    is_chat_model=True,
    context_window=8192,
)
```

```python
# CrewAI
from crewai import LLM

llm = LLM(
    model="openai/default",  # the "openai/" prefix is required by CrewAI
    base_url="http://127.0.0.1:8000/v1",
    api_key="not-needed",
)
```

## What works today

- `POST /v1/chat/completions` — OpenAI-compatible request/response shape,
  including real SSE streaming (`stream: true`, byte-for-byte proxied, not
  buffered) and tool/function calling (`tools`, `tool_choice`,
  `parallel_tool_calls`, streamed tool-call deltas) — single string
  message content, no `n != 1`
- `GET /health`
- One provider adapter, generic over any OpenAI-compatible HTTP endpoint
  (OpenAI itself, or a compatible self-hosted server)
- Static, explicit routing: your request's `model` field selects a named
  route from `inferrail.yaml`
- Retries with backoff for transient provider errors, configurable per
  route
- A structured telemetry record for every request — latency, tokens,
  retries, success/failure — logged locally (console or JSONL)
- A payload-free economic receipt for every request, with verified
  `Decimal` pricing and business attribution
- `inferrail report --by <dimension>` for local cost aggregation
- A CLI: `inferrail demo`, `inferrail try`, `inferrail serve`
  (`--quickstart`), `inferrail config check`, `inferrail report`

**Not yet supported:** multi-provider intelligent routing, cost estimates
for models outside the built-in catalog or an explicit `pricing:`
override, budgets/spend limits, any provider that isn't OpenAI-compatible.
Full list in [docs/PRODUCT.md](docs/PRODUCT.md).

## Configure

For a real deployment with explicit, checked-in configuration:

```bash
cp inferrail.example.yaml inferrail.yaml
cp .env.example .env            # then edit .env with a real OPENAI_API_KEY
inferrail config check          # validate without starting a server
inferrail serve
```

`inferrail.yaml` only ever references the *name* of the environment
variable holding a secret, never the secret itself — see
[inferrail.example.yaml](inferrail.example.yaml) for the full shape
(providers, routes, telemetry, receipts, pricing overrides, server). By
default `inferrail serve` starts on `http://127.0.0.1:8000`.

## Cost and attribution

Every request's receipt (one JSON line in `inferrail-receipts.jsonl`):

```json
{
  "receipt_id": "ir_1e6c916bac8940ca8a85",
  "request_id": "req_62f023c643fd4f4285d1",
  "timestamp": "2026-08-16T06:12:23.486756Z",
  "route": "default",
  "provider": "openai",
  "model": "gpt-4o-mini",
  "status": "success",
  "prompt_tokens": 842,
  "completion_tokens": 191,
  "pricing": {
    "input_usd_per_million": "0.15",
    "output_usd_per_million": "0.60",
    "source": "https://developers.openai.com/api/docs/pricing",
    "verified_date": "2026-08-16"
  },
  "estimated_cost_usd": "0.000241",
  "attributes": { "customer": "acme", "workflow": "contract-review" },
  "total_latency_ms": 15.96,
  "retry_count": 0
}
```

Pricing comes from a small built-in catalog, independently verified
against OpenAI's own published pricing, applied only to a verifiably real
`type: openai` provider — never guessed onto a same-shaped
`openai_compatible` endpoint that could be running something else. An
unresolvable price leaves `pricing`/`estimated_cost_usd` as `null`, never
a fabricated `$0`. All arithmetic uses `Decimal`, never `float`. Full
resolution order:
[docs/PRODUCT.md](docs/PRODUCT.md#cost-and-receipts); full reasoning:
[docs/adr/0005](docs/adr/0005-privacy-preserving-economic-receipts.md).

## For agents

This repo is structured for agent consumption as well as human use — see
`llms.txt`, `openapi.json`, and `config.schema.json` at the repo root.

An MCP server (`inferrail-mcp/`) exposes the local receipt ledger to any
MCP-aware agent — Claude Code, Claude Desktop, Cursor — as two read-only
tools: `get_spend` (query attributed cost by customer/model/route/time
window) and `get_health` (gateway reachability + most recent receipt).
Neither tool can execute inference or spend provider budget. See
[inferrail-mcp/README.md](inferrail-mcp/README.md) for the exact
contract.

```bash
pip install -e ".[mcp]"
```

```json
{
  "mcpServers": {
    "inferrail": {
      "command": "inferrail-mcp"
    }
  }
}
```

- **Claude Code**: add the block above to `.mcp.json` in the project
  root, or run `claude mcp add inferrail -- inferrail-mcp`.
- **Claude Desktop**: add it to `claude_desktop_config.json` (Settings →
  Developer → Edit Config).
- **Cursor**: add it to `.cursor/mcp.json` in the project root, or via
  Cursor Settings → MCP.

## Security

Inferrail is a **local development gateway by default**: it binds to
`127.0.0.1` and, unless you configure otherwise, accepts requests from
anything that can reach that port with no authentication of its own —
this matters because it holds your provider credentials. To require
callers to authenticate, set `INFERRAIL_GATEWAY_TOKEN` (see
`.env.example`) before starting the gateway; requests must then include
`Authorization: Bearer <token>`. This is a single shared-secret check, not
a user/auth system. Full detail: [SECURITY.md](SECURITY.md).

## Privacy

By default, and structurally, Inferrail never sends anything to an
Inferrail-operated service — there isn't one yet. Local telemetry
(`InferenceEvent`) and economic receipts (`InferenceReceipt`) are both
schema-limited to operational/economic metadata; neither has a field that
can hold prompt or response content. See
[docs/adr/0003](docs/adr/0003-no-payload-persistence-by-default.md) and
[docs/adr/0005](docs/adr/0005-privacy-preserving-economic-receipts.md).

What Inferrail *does* send off-machine: your configured provider (e.g.
OpenAI) still receives the actual prompt, exactly as it would if you
called it directly — Inferrail is a pass-through gateway to that provider,
not a privacy boundary against it.

### Verify it yourself

Don't take the no-payload-persistence claim on faith — check it in under a
minute (this checks only what Inferrail itself writes to disk; your
provider still receives the real prompt either way, per above):

```bash
inferrail try "MARKER-1234-do-not-persist-me"
grep -c "MARKER-1234" inferrail-receipts.jsonl    # 0, every time
cat inferrail-receipts.jsonl                      # tokens, cost, pricing — no message content
```

Same result against a running `inferrail serve` — see
[docs/PRODUCT.md](docs/PRODUCT.md#verifying-privacy-claims-yourself) for
the curl + `telemetry.sink: jsonl` walkthrough.

## Why "Inferrail"

The rails on which AI inference runs. v0.1 is a self-hosted gateway; the
long-term thesis — an inference control plane — is in
[docs/PRODUCT.md](docs/PRODUCT.md), and how the pieces fit together in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Contributing and tests

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for dev
setup and the checks CI runs (`ruff`, `mypy`, `pytest`; no API key or
network access required for the default test suite).

## License

Apache License 2.0 — see [LICENSE](LICENSE).
