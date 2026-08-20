# Inferrail

<!-- mcp-name: io.github.domondi1/inferrail -->

A self-hosted, OpenAI-compatible gateway that sits between your app and
your LLM provider, so every request produces a payload-free economic
receipt — measured cost and business attribution, with no prompt or
response ever stored.

[![CI](https://github.com/domondi1/inferrail/actions/workflows/ci.yml/badge.svg)](https://github.com/domondi1/inferrail/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Point your existing OpenAI client at Inferrail instead of directly at
your provider. Nothing else changes — same request/response shape,
same streaming, same tool calls — except now every call gets logged
locally with its real token usage, its verified cost, and whatever
customer/workflow/feature you attribute it to.

**Developer preview (v0.1.1).** Works today; CLI flags, config shape, and
receipt fields may still change before 1.0 — see
[docs/PRODUCT.md](docs/PRODUCT.md).

## Why Inferrail

- **Know what each request cost, and who it was for** — without adding a
  hosted observability vendor or logging prompts yourself.
- **One place to point an OpenAI-compatible client** instead of
  provider-specific SDK code sprinkled through your app.
- **Runs entirely on your own machine.** No Inferrail-operated service
  exists yet, and none of this depends on one.
- **Structurally can't store your prompts.** The receipt and telemetry
  schemas have no field capable of holding message content — not a
  setting, a guarantee.

## Install

```bash
pip install inferrail
```

Requires Python 3.11+.

## Quickstart

See the whole pipeline — request → receipt → cost report — with no API
key and no network call:

```bash
inferrail demo
```

Runs canned requests through Inferrail's real engine using a fake
in-memory provider. Every price and response is clearly labeled `DEMO`.

To try it for real, with your own key:

```bash
export OPENAI_API_KEY=<your-openai-api-key>
inferrail try "Reply with one word: ready" --customer acme
```

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

`inferrail report --by customer` (or `provider`, `model`, `route`, or
any attribute you've attached) aggregates every receipt written so far
into a table of requests, tokens, and cost.

## Send a request

Run the gateway itself with the same zero-config defaults:

```bash
inferrail serve --quickstart
```

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Inferrail-Attribute-Customer: acme" \
  -d '{
    "model": "default",
    "messages": [{"role": "user", "content": "Say hello in five words."}]
  }'
```

The response is standard OpenAI `choices`/`usage` plus a non-standard
`inferrail` block (route, provider, latency, retries) any OpenAI client
already ignores. `X-Inferrail-Attribute-*` headers are optional
attribution — never forwarded upstream. See
[examples/basic_chat_request.py](examples/basic_chat_request.py) for a
minimal Python client, or point any existing OpenAI-compatible SDK
(LangChain, LlamaIndex, CrewAI, ...) at `http://127.0.0.1:8000/v1`.

<details>
<summary>Framework examples (LangChain, LlamaIndex, CrewAI)</summary>

```python
# LangChain
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="not-needed",  # or your INFERRAIL_GATEWAY_TOKEN if auth is enabled
    model="default",
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
    model="openai/default",  # "openai/" prefix required by CrewAI
    base_url="http://127.0.0.1:8000/v1",
    api_key="not-needed",
)
```

</details>

## What Inferrail records

Every request writes one payload-free JSON receipt — no field on it can
hold a prompt or response:

```json
{
  "receipt_id": "ir_1e6c916bac8940ca8a85",
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

If Inferrail can't verify a price for the (provider, model) pair, `pricing`
and `estimated_cost_usd` are `null` — never a guessed or fabricated cost.
All money math uses `Decimal`, never `float`. Details:
[docs/adr/0005](docs/adr/0005-privacy-preserving-economic-receipts.md).

## Model routing

`"model"` normally selects a named route from `inferrail.yaml` (e.g.
`"default"`), which maps to a provider + underlying model. If
`default_provider` is set in your config, a `model` that matches no route
is instead forwarded to that provider unchanged — so `"model":
"gpt-5.6-sol"` works with no route pre-registered for it. Named routes
always take priority. This passthrough is on by default for the
zero-config quickstart path, off by default otherwise. Full design:
[docs/adr/0007](docs/adr/0007-model-passthrough-routing.md).

## MCP

```bash
pip install "inferrail[mcp]"
```

An MCP server (`inferrail-mcp`), published on the MCP registry as
[`io.github.domondi1/inferrail`](https://registry.modelcontextprotocol.io),
exposes Inferrail's local receipt ledger to any MCP-aware agent (Claude
Code, Claude Desktop, Cursor, ...) as two **read-only** tools — neither
executes inference or spends provider budget:

| Tool | What it does |
|---|---|
| `get_spend` | Aggregates local receipts by provider/model/route/attribute, optional time window |
| `get_health` | Checks gateway reachability + most recent local receipt |

```json
{
  "mcpServers": {
    "inferrail": { "command": "inferrail-mcp" }
  }
}
```

Claude Code: `claude mcp add inferrail -- inferrail-mcp`. Full contract:
[inferrail-mcp/README.md](inferrail-mcp/README.md).

## Configuration

For a real deployment instead of quickstart defaults:

```bash
cp inferrail.example.yaml inferrail.yaml
cp .env.example .env      # then add a real OPENAI_API_KEY
inferrail config check    # validate without starting a server
inferrail serve
```

`inferrail.yaml` only ever holds the *name* of an environment variable
for a secret, never the secret itself. Full shape (providers, routes,
telemetry, receipts, pricing overrides):
[inferrail.example.yaml](inferrail.example.yaml).

By default the gateway binds to `127.0.0.1:8000` with no auth. Set
`INFERRAIL_GATEWAY_TOKEN` to require callers to send `Authorization:
Bearer <token>` — see [SECURITY.md](SECURITY.md).

## What works today

- `POST /v1/chat/completions`: streaming (`stream: true`, real SSE
  passthrough) and tool/function calling, single string message content,
  no `n != 1`
- `GET /health`
- One provider adapter, generic over any OpenAI-compatible HTTP endpoint
- Named-route + optional passthrough model routing (above)
- Per-route retry with backoff on transient provider errors
- Local structured telemetry and payload-free cost receipts for every
  request, plus `inferrail report --by <dimension>`
- CLI: `inferrail demo`, `try`, `serve` (`--quickstart`), `config check`,
  `report`

**Not yet:** multi-provider intelligent routing, cost estimates for
models outside the built-in catalog or an explicit `pricing:` override,
budgets/spend limits, any non-OpenAI-compatible provider. Full scope:
[docs/PRODUCT.md](docs/PRODUCT.md).

## Documentation

- [docs/PRODUCT.md](docs/PRODUCT.md) — exact current scope
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — package layout, request
  lifecycle
- [docs/adr/](docs/adr/) — why specific structural decisions were made
- [openapi.json](openapi.json) / [config.schema.json](config.schema.json)
  / [llms.txt](llms.txt) — machine-readable references for tooling and
  agents
- [SECURITY.md](SECURITY.md)

## Development

```bash
git clone https://github.com/domondi1/inferrail.git && cd inferrail
pip install -e ".[dev,mcp]"
ruff check . && mypy && pytest
```

`pytest` needs no API key or network access — see
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE).
