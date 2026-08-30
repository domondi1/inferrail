# Inferrail

<!-- mcp-name: io.github.domondi1/inferrail -->

Know what your AI work costs.

Inferrail turns supported OpenAI chat-completion traffic into local,
attributable economic receipts. Give related requests a customer-defined
`work_id`, declare an outcome when your application knows one, and inspect the
known inference economics associated with that work without storing prompts,
responses, or tool payloads in Inferrail's own records.

[![CI](https://github.com/domondi1/inferrail/actions/workflows/ci.yml/badge.svg)](https://github.com/domondi1/inferrail/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/inferrail.svg)](https://pypi.org/project/inferrail/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

For the supported chat-completions surface, Inferrail records known cost when
measured usage and a verified price are available. Otherwise it reports
`unknown`, never a fabricated `$0`.

## 30-second demo

**Current main / upcoming Work Economics release.** Work Economics was added
after the current PyPI release. To try the current product before the next
release, install from `main`:

```bash
pip install "inferrail @ git+https://github.com/domondi1/inferrail.git@main"
inferrail demo
```

The demo needs no API key, no network call, and no provider billing. It runs
canned requests through Inferrail's real engine with made-up prices labeled
`DEMO`, then shows receipts, attribution, work-level economics, and explicit
unknown evidence.

**Stable PyPI release.** `pip install inferrail` currently installs `0.1.2`.
It includes the gateway, receipts, reports, and `TaskTransaction`, but not the
new `work` commands. It remains the stable released install until the next
package publication.

## What just happened?

```text
AI request
  -> InferenceReceipt
  -> caller-supplied attribution
  -> related requests share work_id
  -> customer-declared outcome
  -> Work Economics
```

- **Receipt:** one inference request produced payload-free economic evidence.
- **Attribution:** the caller can attach identifiers such as customer,
  workflow, or project.
- **Work:** several requests can share a `work_id` that your application
  defines.
- **Outcome:** your application can append a declaration of what happened to
  that work.
- **Work Economics:** Inferrail joins that declaration with matching receipts
  and reports known attributed inference economics for the work.

You decide what a unit of work means: a contract review, support resolution,
coding task, research run, or document-processing job. Inferrail associates
economic evidence with the identifier your application supplies; it does not
interpret the business meaning of that identifier or its outcome.

### Request economics vs. Work Economics

**Request economics:** what known inference economics belong to one request?

**Work Economics:** what known inference economics belonged to the
customer-defined unit of work those requests were performing?

This is not a full cost of work, COGS, margin, or business-value calculation.

## Track a unit of work

The following uses real provider requests and requires `OPENAI_API_KEY`:

```bash
export OPENAI_API_KEY=<your-openai-api-key>

inferrail try "Review this contract clause" \
  -a work_id=contract_review_42

inferrail try "Identify remaining risks" \
  -a work_id=contract_review_42

inferrail work outcome contract_review_42 --status completed
inferrail work contract_review_42
inferrail work --all
```

For a gateway client, the equivalent generic attribution header is:

```text
X-Inferrail-Attribute-Work-Id: contract_review_42
```

The deterministic offline demo includes this synthetic example:

```text
work-contract-1
  2 inference receipts
  customer-declared outcome: resolved
  known attributed inference cost: $0.000483
```

`resolved` is only the demo application's own outcome meaning. Inferrail does
not treat any outcome status as universally successful.

If Inferrail cannot verify the price for an observed inference event, its cost
remains `unknown` rather than being treated as zero. No receipt evidence is
also not the same thing as known zero cost.

## First real request and reports

`inferrail try` is the shortest route to one real receipt. It uses your
existing `OPENAI_API_KEY`; if it is not set, Inferrail prints what is required.
It prints the response, receipt, measured tokens, known cost or `unknown`, the
local receipt path, and the next report command.

```bash
inferrail try "Reply with one word: ready" --customer acme
inferrail report
inferrail report --by customer
inferrail report --by workflow
inferrail report --by provider
```

## What a receipt contains

One payload-free JSON receipt per supported request:

```json
{
  "receipt_id": "ir_1e6c916bac8940ca8a85",
  "provider": "openai",
  "model": "gpt-4o-mini",
  "prompt_tokens": 842,
  "completion_tokens": 191,
  "estimated_cost_usd": "0.000241",
  "attributes": { "customer": "acme", "workflow": "contract-review" }
}
```

(Trimmed — the full record also carries pricing provenance, status,
route, timestamp, latency, and retry count. See
[Privacy boundary](#privacy-boundary) below for the complete shape.)

## TaskTransaction: receipt-only task grouping

One task is rarely one call. Tag every request belonging to one unit of
work with the same attribution value, then ask Inferrail what the task
cost:

```bash
export OPENAI_API_KEY=<your-openai-api-key>
inferrail try "Reply with one word: ready" -a task_id=bug_9281
inferrail try "Summarize: the retry patch is deployed" -a task_id=bug_9281
inferrail transaction bug_9281
```

```
Task:        bug_9281
Transaction: tx_72fcfcca9ede9d2facc3
Status:      success

EVENT TYPE  EVENT ID                 STATUS   COST
inference   ir_f6fb6403d5324ea0acf9  success  $0.000003
inference   ir_756cc072a27f42f4a2ea  success  $0.000007

Known total cost: $0.00001
```

This TaskTransaction example uses real provider requests and a `task_id`.
The offline demo instead correlates requests with `work_id` and shows Work
Economics. Over HTTP, an
`X-Inferrail-Attribute-Task-Id: bug_9281` header does the same thing;
`inferrail.track_task(task_id=...)` (see [Attribute spend](#attribute-spend)
below) attaches it automatically to every nested call in an agent run, no
header-threading required. See
[docs/adr/0008](docs/adr/0008-task-transactions.md).

## Use it as a gateway

For a long-running application, start the separate gateway process. The
gateway process must have access to the provider credential through the
configured environment variable; a key held only inside application memory is
not automatically transferred to the gateway.

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
minimal Python client, or point a supported OpenAI-compatible chat client at
`http://127.0.0.1:8000/v1`. An OpenAI SDK client that does not set `base_url`
can use its existing `OPENAI_BASE_URL` environment mechanism instead.

The default receipt is one JSONL line per supported request in
`./inferrail-receipts.jsonl`, relative to the gateway's working directory.
Treat that file as machine/audit evidence; use `inferrail report` for the
human aggregate, `inferrail transaction <task-id>` for receipt-only task
grouping, and `inferrail work <work-id>` for work-attributed inference
economics plus a customer-declared outcome.

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

`"model"` normally selects a named route from `inferrail.yaml` (e.g.
`"default"`), which maps to a provider + underlying model. If
`default_provider` is set in your config, a `model` that matches no route
is instead forwarded to that provider unchanged — so `"model":
"gpt-5.6-sol"` works with no route pre-registered for it. Named routes
always take priority. This passthrough is on by default for the
zero-config quickstart path, off by default otherwise. Full design:
[docs/adr/0007](docs/adr/0007-model-passthrough-routing.md).

## Attribute spend

Three ways to attach business context to a request, all landing in the
same `attributes: dict[str, str]` on its receipt:

- **HTTP header** (gateway): `X-Inferrail-Attribute-<Name>: <value>`, e.g.
  `X-Inferrail-Attribute-Task-Id: bug_9281`.
- **CLI flag** (`inferrail try`): `--customer`/`--workflow` shorthand, or
  generic `-a <name>=<value>` for anything else, including `task_id`.
- **Ambient, for nested agent calls**: `inferrail.track_task` attaches
  `X-Inferrail-Attribute-Task-Id` to every outgoing request for the
  duration of a `with` block or decorated function — no threading a
  `task_id` parameter through nested function signatures by hand.

```python
import inferrail
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="not-needed",
    # also accepted by LangChain's ChatOpenAI, CrewAI's LLM, etc. via
    # their own http_client= argument
    # base_url must match the client's own base_url above — the header is
    # only ever attached to requests going to that destination.
    http_client=inferrail.attributed_http_client(base_url="http://127.0.0.1:8000/v1"),
)

@inferrail.track_task(task_id="bug_9281")
def fix_bug():
    client.chat.completions.create(...)  # tagged automatically
    run_subagent()  # nested calls too — no task_id parameter needed
```

`with inferrail.track_task(task_id="..."):` works the same way. Sync and
async are both supported (`attributed_async_http_client(base_url=...)` for
`AsyncOpenAI`/async frameworks); concurrent tasks never cross-contaminate.
This is a small client-side convenience over the HTTP header above — no
gateway or schema change, `task_id` only, no public API stability
commitment yet. See
[docs/adr/0009](docs/adr/0009-ambient-task-tracking.md).

Once tagged, `inferrail report` shows the all-up aggregate, while
`inferrail report --by <provider|model|route|attribute-name>`
aggregates receipts by any of these dimensions —
`customer`, `workflow`, `task_id`, or anything else you've attached.

## Referral early access

Referral access is opening soon. Planned early-access rewards are based on
verified routed usage, not signup:

```text
1 verified referral
→ +90 days of cost history for both sides

3 verified referrals
→ Pro for one year + unlimited seats

10 verified referrals
→ Founding Operator
→ permanent Pro
→ logo on the site
→ roadmap vote
→ private channel

25 verified referrals
→ Inferrail free for life
→ 20% recurring on additional teams referred
```

Program terms will be published when referral access opens.

See the current program presentation at [tryinferrail.com](https://tryinferrail.com).

## How it works

`InferenceEngine` normalizes the request, resolves `model` to a route in
`inferrail.yaml` (a pure config lookup — no cost/latency-aware
selection in v0.1), calls the one provider adapter in this version
(`OpenAIProvider`, generic over `base_url` — OpenAI itself, Azure
OpenAI's compatible surface, vLLM, llama.cpp-server, or anything else
speaking the same wire format), and emits a telemetry event and a
receipt for every supported request, success or failure. Full lifecycle, package
layout, and the streaming/retry boundaries:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Privacy boundary

Inferrail's own local receipt, telemetry, and outcome records contain
economic metadata and caller-supplied identifiers, not persisted prompts,
responses, tool payloads, or free-form business outcome payloads.
Structurally, the receipt and telemetry schemas have no field capable of
holding message content, and
`test_inference_receipt_has_no_payload_fields` enforces it. This is a
claim about **Inferrail's own local records**, not about the request path as
a whole — your configured provider still receives the real prompt either
way; Inferrail is a pass-through gateway to it, not a privacy boundary
against the provider.

Inferrail currently measures supported OpenAI chat-completions traffic. It is
not a background monitor: it records while requests pass through the running
process and serves nothing when that process is stopped. It does not enforce
budgets or control provider spend.

`inferrail try` says this in its own output too, not just in the schema:

```
  Prompt stored     no
  Response stored   no
```

The full receipt shape, all fields:

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

If Inferrail can't verify a price for the (provider, model) pair,
`pricing` and `estimated_cost_usd` are `null` — never a guessed or
fabricated cost. You can check the no-payload claim yourself against a
running gateway, not just take it on faith:
[docs/PRODUCT.md's verification walkthrough](docs/PRODUCT.md#verifying-privacy-claims-yourself).
Design rationale:
[docs/adr/0005](docs/adr/0005-privacy-preserving-economic-receipts.md).

## MCP

```bash
pip install "inferrail[mcp]"
```

An MCP server (`inferrail-mcp`), published on the MCP registry as
[`io.github.domondi1/inferrail`](https://registry.modelcontextprotocol.io),
exposes Inferrail's local receipt ledger to any MCP-aware agent (Claude
Code, Claude Desktop, Cursor, ...) as two **read-only** tools — neither
executes inference nor spends provider budget:

| Tool | What it does |
|---|---|
| `get_spend` | Aggregates local receipts by provider/model/route/attribute (including `task_id`), optional time window |
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

## Supported today

- `POST /v1/chat/completions`: streaming (`stream: true`, real SSE
  passthrough) and tool/function calling, single string message content,
  no `n != 1`
- `GET /health`
- One provider adapter, generic over any OpenAI-compatible HTTP endpoint
- Named-route + optional passthrough model routing (above)
- Per-route retry with backoff on transient provider errors
- Local structured telemetry and payload-free cost receipts for supported
  requests, plus `inferrail report`, grouped reports, and
  `inferrail transaction <task-id>`
- Customer-defined `work_id` attribution, append-only outcome declarations,
  and derived Work Economics via `inferrail work outcome`, `inferrail work
  <work-id>`, and `inferrail work --all`
- CLI: `inferrail demo`, `try`, `serve` (`--quickstart`), `config check`,
  `report`, `transaction`, `work`

## Not yet

Honest edges, not silent gaps — full list in
[docs/PRODUCT.md](docs/PRODUCT.md):

- Cost- or latency-aware routing, or automatic failover to a different
  provider/model on error — routing is a static config lookup
- Budgets, spend limits, or blocking a request based on cost
- Any provider whose wire protocol isn't OpenAI-compatible (native
  Anthropic, Gemini, Bedrock, ...)
- The full OpenAI API surface — only `/v1/chat/completions` and
  `/health` exist; no embeddings, assistants, batch, images, or audio
- Multi-user auth or role-based access control —
  `INFERRAIL_GATEWAY_TOKEN` is one shared secret, not a user system
- Any hosted or cloud-operated component
- Non-LLM economic events (browser, search, compute/sandbox, MCP tool
  cost) in a `TaskTransaction` — its only event type today is `inference`
- Outcome or business-value linkage (success signal, revenue, margin) on
  a `TaskTransaction` — it aggregates cost only

## Deployment boundary

**Single node.** The receipt ledger is a local append-only JSONL file, so
every process that should appear in one report must write to one file on
one filesystem.

- Concurrent writers to the same file are safe: each receipt is written
  as a single atomic `O_APPEND` write, so threads *and* multiple
  processes on the same host can share one ledger without interleaving or
  losing records.
- Not supported: several hosts writing to one ledger, aggregating ledgers
  across machines, or anything resembling a shared/hosted control plane.
  Running Inferrail on N hosts gives you N separate ledgers, and nothing
  in the product merges them.
- `inferrail report` and `inferrail transaction` read the whole file into
  memory. That is fine for the millions-of-bytes range a developer
  preview produces; it is not a query engine, and there is no retention,
  rotation, or compaction. Rotate the file yourself if it grows.

Anything beyond one host is out of scope for v0.x — see
[docs/PRODUCT.md](docs/PRODUCT.md).

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
