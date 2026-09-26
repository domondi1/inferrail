# Integrations

How to send traffic through a self-hosted Inferrail gateway, attach
attribution, and group requests into units of work. For the exact
supported request surface, see [PRODUCT.md](PRODUCT.md).

## Before you start

The gateway process calls the provider, so **the gateway process needs
the provider key**. A key that exists only in your application's process
is not passed along. Set the key in the terminal where you run
`inferrail serve`:

```bash
export OPENAI_API_KEY=...        # for /v1/chat/completions
export ANTHROPIC_API_KEY=...     # for /v1/messages
inferrail serve --quickstart
```

`--quickstart` registers both providers and passes any model id through
to the matching one. Only the provider whose key is set will succeed.
Real requests are billed by your provider as usual.

Clients then point at the gateway. Unless you set
`INFERRAIL_GATEWAY_TOKEN`, the gateway ignores the client's API key, so
any placeholder works.

## OpenAI SDK and compatible clients

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="not-needed")
client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Say hello in five words."}],
    extra_headers={"X-Inferrail-Attribute-Customer": "acme"},
)
```

Or, without code changes: `export OPENAI_BASE_URL=http://127.0.0.1:8000/v1`.

With curl:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Inferrail-Attribute-Customer: acme" \
  -d '{"model": "gpt-4o-mini",
       "messages": [{"role": "user", "content": "Say hello in five words."}]}'
```

The response is the standard `choices`/`usage` shape plus a non-standard
`inferrail` block (route, provider, latency, retries) that OpenAI clients
ignore. Supported: text messages, streaming (`stream: true`), and
tool/function calling. Rejected with an error rather than silently
dropped: `n != 1`, list-shaped (multi-part or image) message content, and
any unmodeled field such as `response_format`. See [examples/basic_chat_request.py](../examples/basic_chat_request.py).

## Anthropic SDK and Claude Code

`POST /v1/messages` is a separate Anthropic-compatible passthrough with
streaming and tool use ([ADR 0014](adr/0014-anthropic-messages-passthrough.md)).
The Anthropic SDKs append `/v1/messages` themselves, so their base URL
stops at the origin, **without** `/v1`:

```python
import anthropic

client = anthropic.Anthropic(base_url="http://127.0.0.1:8000", api_key="not-needed")
client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=[{"role": "user", "content": "Say hello in five words."}],
)
```

Or `export ANTHROPIC_BASE_URL=http://127.0.0.1:8000`, which is also how
you point Claude Code at the gateway. See
[examples/anthropic_messages_request.py](../examples/anthropic_messages_request.py).

> The `inferrail serve --quickstart` banner in release 0.4.3 prints the
> Anthropic base URL with a trailing `/v1`, which makes the SDK request
> `/v1/v1/messages`. Use the origin shown above. The banner is fixed on
> `main`.

## Choosing a model

`"model"` first selects a named route from `inferrail.yaml` (for example
`default`), which maps to a provider and model. If `default_provider` (or
`default_anthropic_provider`) is set, a model that matches no route is
forwarded unchanged to that provider. Quickstart turns this passthrough
on; explicit configs leave it off unless you set it. Named routes always
win. Design: [ADR 0007](adr/0007-model-passthrough-routing.md).

Cost is computed only when the provider reports usage and a verified
price is on file for that provider and model. Otherwise `pricing` and
`estimated_cost_usd` are `null`, never a guessed `0`. The built-in price
catalog applies only to the providers' own default endpoints; for a
custom `base_url`, add prices under `pricing:` in `inferrail.yaml`.

## Framework configuration

These are configuration examples. CI tests the OpenAI wire protocol these
frameworks use ([test_agent_e2e.py](../tests/unit/test_agent_e2e.py)),
not the frameworks themselves.

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

## Voice agents

Inferrail has no native voice support. It does not handle audio,
speech-to-text, text-to-speech, the OpenAI Realtime API, or WebSocket
sessions, and it does not account for full call cost.

A voice stack can still route its **text LLM stage** through Inferrail
if that stage lets you set an OpenAI- or Anthropic-compatible base URL
and sends a supported request shape (text messages, optionally streaming
or tool calls). Only that stage's tokens and cost appear in receipts.

No voice framework integration (LiveKit, Pipecat, Vapi, Retell, or
others) has been tested by this project. Treat compatibility as
something to verify in your own stack: send one request, then check
that a receipt appears in `inferrail report`.

## Attribution

Three ways to attach business context, all landing in the same
`attributes: dict[str, str]` on the receipt:

- **HTTP header** (gateway): `X-Inferrail-Attribute-<Name>: <value>`, for
  example `X-Inferrail-Attribute-Task-Id: bug_9281`. These headers are
  never forwarded to the provider ([attribution.py](../src/inferrail/gateway/attribution.py)).
- **CLI flag** (`inferrail try`): `--customer`, `--workflow`, or generic
  `-a <name>=<value>`.
- **Ambient, for nested agent calls**: `inferrail.track_task` attaches
  `X-Inferrail-Attribute-Task-Id` to every request inside a `with` block
  or decorated function.

```python
import inferrail
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="not-needed",
    # base_url must match the client's own base_url: the header is only
    # attached to requests going to that destination.
    http_client=inferrail.attributed_http_client(base_url="http://127.0.0.1:8000/v1"),
)

@inferrail.track_task(task_id="bug_9281")
def fix_bug():
    client.chat.completions.create(...)  # tagged automatically
    run_subagent()  # nested calls too
```

Sync and async are both supported (`attributed_async_http_client`), and
concurrent tasks never cross-contaminate. See
[ADR 0009](adr/0009-ambient-task-tracking.md).

**Attribute values are stored exactly as sent.** Use identifiers, not
names, emails, secrets, or message text.

Then slice spend by any dimension:

```bash
inferrail report --by customer
inferrail report --by workflow
inferrail report --by provider   # also: model, route, or any attribute name
```

## Work and outcomes

Give related requests the same `work_id`, then declare an outcome when
your application knows one:

```bash
inferrail try "Review this contract clause" -a work_id=contract_review_42
inferrail try "Identify remaining risks"   -a work_id=contract_review_42
inferrail work outcome contract_review_42 --status completed
inferrail work contract_review_42
inferrail work --all
```

`inferrail try` sends one real, billed request with `OPENAI_API_KEY`. Over
HTTP, use `X-Inferrail-Attribute-Work-Id: contract_review_42`.

Work Economics reports the **known** attributed inference cost for that
work. If any receipt in the work has unknown cost, the report counts it
separately rather than folding it into the total, so a known subtotal is
not a complete bill. It is not COGS, margin, or business value, and
Inferrail does not interpret your outcome labels.

`inferrail transaction <task-id>` gives the older receipt-only grouping
by `task_id` ([ADR 0008](adr/0008-task-transactions.md)).

## MCP

```bash
pip install inferrail
claude mcp add inferrail -e INFERRAIL_RECEIPTS_PATH=/absolute/path/to/inferrail-receipts.jsonl -- inferrail mcp
```

`inferrail-mcp` exposes the local receipt ledger to MCP clients as two
read-only tools: `get_spend` (aggregate by provider, model, route, or
attribute) and `get_health` (gateway reachability and latest receipt).
Neither runs inference or writes files; receipts store usage and cost
metadata without persisting prompt or response bodies. Client config and receipts-path setup:
[README](../README.md#mcp). Full contract: [inferrail-mcp/README.md](../inferrail-mcp/README.md).
