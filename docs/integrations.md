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

The model ids in the examples below are only examples. Inferrail doesn't
choose a model: send any model your account can use. `inferrail models`
lists them and shows which ones have a price (needed under a dollar
budget).

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
tool/function calling, structured outputs (`response_format`), and the
other provider-valid fields listed in [PRODUCT.md](PRODUCT.md). Rejected
with an error rather than silently dropped: `n != 1`, non-text content
parts, fields whose cost the gateway can't account for, and any unknown
field. See [examples/basic_chat_request.py](../examples/basic_chat_request.py).

## Anthropic SDK

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

Or `export ANTHROPIC_BASE_URL=http://127.0.0.1:8000`. See
[examples/anthropic_messages_request.py](../examples/anthropic_messages_request.py).

### Claude Code

Point Claude Code at the gateway and give a run (or a whole loop of runs)
an id and a dollar budget with `ANTHROPIC_CUSTOM_HEADERS`:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_CUSTOM_HEADERS=$'X-Inferrail-Attribute-Work-Id: loop-42\nX-Inferrail-Budget-Usd: 20'
claude -p "..." --model claude-sonnet-5
inferrail work loop-42
```

Every invocation that sends the same work id draws from the same budget,
so an orchestrator that runs Claude Code round after round (build, review,
fix) can cap the whole loop, not just one session. When the budget can't
cover the next request, the gateway refuses it with HTTP 402 before the
provider and Claude Code stops with
`API Error: 402 budget 'work_id:loop-42:per_work' ... would be exceeded`.

Things to know:

- Claude Code sends `max_tokens: 128000`, and each request reserves its
  worst-case cost from that, so size budgets well above one request's
  ceiling. The unused part is released as soon as the response settles.
- The model needs a price. Claude Code's default model may not be in the
  built-in catalog yet; pick one that is with `--model` (see
  `inferrail models`) or add a `pricing:` override.

Tested with Claude Code 2.1.289 (requests accepted and forwarded,
refusals stop the session, two invocations sharing one budget) against a
stand-in upstream; not yet confirmed end to end against the Anthropic API.

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

## Agents and apps

Config-only setups for tools that make many model calls per task. Each
gives one run (or session) an id and a dollar ceiling; `inferrail work
<id>` and `inferrail report --by work_id` show what it cost. All were
run against a stand-in OpenAI upstream with the versions noted (no
provider spend); "on refusal" is what the tool does when the gateway
returns 402.

### goose (per session)

A custom provider can send goose's session id under any header name:

```json
{
  "name": "inferrail",
  "engine": "openai",
  "display_name": "OpenAI via Inferrail",
  "api_key_env": "",
  "requires_auth": false,
  "base_url": "http://127.0.0.1:8000/v1",
  "models": [{"name": "gpt-4o-mini", "context_limit": 128000}],
  "headers": {"X-Inferrail-Budget-Usd": "2.00"},
  "session_id_header_override": "X-Inferrail-Attribute-Work-Id"
}
```

Save as `~/.config/goose/custom_providers/inferrail.json` and select the
`inferrail` provider. goose v1.53.0. On refusal: stops with its generic
"add more credits" message, no retry loop.

### Qwen Code (per session)

```json
{
  "modelProviders": {
    "openai": [{
      "id": "gpt-4o-mini",
      "envKey": "INFERRAIL_PLACEHOLDER_KEY",
      "baseUrl": "http://127.0.0.1:8000/v1",
      "generationConfig": {
        "customHeaders": {
          "X-Inferrail-Attribute-Work-Id": "qwen-${session_id}",
          "X-Inferrail-Budget-Usd": "2.00"
        },
        "samplingParams": {"max_tokens": 4000}
      }
    }]
  },
  "outboundCorrelation": {"allowDynamicHeaderValues": true}
}
```

In `~/.qwen/settings.json`, with `INFERRAIL_PLACEHOLDER_KEY=unused`
exported. Qwen Code 0.25.0. On refusal: `[API Error: 402 budget ...]`,
exit 1, one request.

### Crush (per run)

`extra_headers` expands environment variables and drops a header that
expands to empty, so a run is capped only when you name it:

```json
{
  "providers": {
    "capped": {
      "type": "openai-compat",
      "base_url": "http://127.0.0.1:8000/v1",
      "api_key": "unused",
      "extra_headers": {
        "X-Inferrail-Attribute-Work-Id": "${INFERRAIL_WORK_ID}",
        "X-Inferrail-Budget-Usd": "${INFERRAIL_WORK_ID:+${INFERRAIL_BUDGET_USD:-2.00}}"
      },
      "models": [{"id": "gpt-4o-mini", "name": "gpt-4o-mini",
                  "context_window": 128000, "default_max_tokens": 4000}]
    }
  }
}
```

`INFERRAIL_WORK_ID=ticket-4411 crush run "..."`. crush v0.97.1. On
refusal: `payment required: budget ...`, exit 1, no retries.

### OpenCode (per day)

Provider headers are static, so cap a project per day:

```json
{
  "provider": {
    "capped": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "unused",
        "headers": {"X-Inferrail-Attribute-Project": "opencode"}
      },
      "models": {"gpt-4o-mini": {"name": "gpt-4o-mini"}}
    }
  },
  "model": "capped/gpt-4o-mini"
}
```

```bash
inferrail budget set --scope project --scope-value opencode --window daily --mode block --limit-usd 5
```

opencode 1.18.34. On refusal: prints the budget error and exits, two
requests, no retry loop.

### PR-Agent (per review run, GitHub Actions)

Run the gateway and PR-Agent's CLI in the same job:

```yaml
- run: pip install pr-agent inferrail
- env: {OPENAI_API_KEY: "${{ secrets.OPENAI_KEY }}"}
  run: |
    nohup inferrail serve --quickstart --app-mode --no-telemetry > gateway.log 2>&1 &
    curl -s --retry 20 --retry-connrefused --retry-delay 1 http://127.0.0.1:8000/health
- env:
    GITHUB__USER_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    OPENAI__KEY: unused
    OPENAI__API_BASE: http://127.0.0.1:8000/v1
    CONFIG__MODEL: gpt-5
    LITELLM__EXTRA_HEADERS: '{"X-Inferrail-Attribute-Work-Id": "pr-${{ github.event.pull_request.number }}", "X-Inferrail-Budget-Usd": "0.50"}'
  run: python -m pr_agent.cli --pr_url "${{ github.event.pull_request.html_url }}" review
```

pr-agent 0.47.0. On refusal: two attempts, then `Failed to review PR`.

### crawl4ai, ScrapeGraphAI, Prefect, Atomic Agents (per job)

```python
# crawl4ai: one id per crawl job
LLMExtractionStrategy(
    llm_config=LLMConfig(provider="openai/gpt-4o-mini", api_token="unused",
                         base_url="http://127.0.0.1:8000/v1"),
    extra_args={"max_tokens": 800, "extra_headers": {
        "X-Inferrail-Attribute-Work-Id": job_id, "X-Inferrail-Budget-Usd": "2.00"}},
    instruction="...",
)

# ScrapeGraphAI: pass a ChatOpenAI as model_instance
config = {"llm": {"model_instance": ChatOpenAI(
    model="gpt-4o-mini", base_url="http://127.0.0.1:8000/v1", api_key="unused",
    max_tokens=800, default_headers={"X-Inferrail-Attribute-Work-Id": job_id,
                                     "X-Inferrail-Budget-Usd": "1.50"}),
    "model_tokens": 128000}}

# Prefect: the flow run id is the work id
OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused", default_headers={
    "X-Inferrail-Attribute-Work-Id": f"prefect-{prefect.runtime.flow_run.id}",
    "X-Inferrail-Budget-Usd": "5.00"})

# Atomic Agents: one instructor client per request, shared by every agent
instructor.from_openai(OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused",
    default_headers={"X-Inferrail-Attribute-Work-Id": request_id,
                     "X-Inferrail-Budget-Usd": "0.25"}))
```

crawl4ai 0.9.4, scrapegraphai 2.3.0, prefect 3.8.7, atomic-agents 2.10.3.
**Retries:** a refused call can't pass on retry, but some retry layers
resend it anyway (free, since refusals never reach the provider, but
wasted). Prefect `@task(retries=...)` and instructor's default
`max_retries` both do; skip 402 with a Prefect `retry_condition_fn`, and
pass instructor `max_retries=Retrying(stop=stop_after_attempt(3),
retry=retry_if_exception_type(ValidationError))`.

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
inferrail try "Review this contract clause" --model <model id> -a work_id=contract_review_42
inferrail try "Identify remaining risks"   --model <model id> -a work_id=contract_review_42
inferrail work outcome contract_review_42 --status completed
inferrail work contract_review_42
inferrail work --all
```

`inferrail try` sends one real, billed request with `OPENAI_API_KEY` to
the model you name (`inferrail models` lists yours). Over
HTTP, use `X-Inferrail-Attribute-Work-Id: contract_review_42`.

Work Economics reports the **known** attributed inference cost for that
work. If any receipt in the work has unknown cost, the report counts it
separately rather than folding it into the total, so a known subtotal is
not a complete bill. It is not COGS, margin, or business value, and
Inferrail does not interpret your outcome labels.

`inferrail transaction <task-id>` gives the older receipt-only grouping
by `task_id` ([ADR 0008](adr/0008-task-transactions.md)).

## MCP

`inferrail mcp` is a stdio MCP server with two read-only tools over your
local receipts file:

| Tool | What it answers |
|---|---|
| `get_spend` | Known cost, tokens, and request counts grouped by `provider`, `model`, `route`, or any attribute you tag requests with (`customer`, `workflow`, `work_id`), optionally within a time window. Requests with unknown pricing are counted separately, not as `$0`. |
| `get_health` | Whether the gateway answers `GET /health`, plus the most recent receipt. |

Neither runs inference, spends provider budget, changes configuration, or
writes files. Receipts store usage and cost metadata without persisting
prompt or response bodies, so the tools have none to return. Grouping by
`customer`, `workflow`, or `work_id` only covers requests that were sent
with that tag ([Attribution](#attribution)).

Claude Code:

```bash
claude mcp add inferrail -e INFERRAIL_RECEIPTS_PATH=/absolute/path/to/inferrail-receipts.jsonl -- uvx --with "mcp>=2.0" inferrail mcp
```

Claude Desktop, Cursor, and other clients that use `mcpServers` (VS Code
uses the same entry under `servers`):

```json
{
  "mcpServers": {
    "inferrail": {
      "command": "uvx",
      "args": ["inferrail", "mcp"],
      "env": {
        "INFERRAIL_RECEIPTS_PATH": "/absolute/path/to/inferrail-receipts.jsonl"
      }
    }
  }
}
```

Without `uvx`: `pip install inferrail`, then use `inferrail mcp` as the
command.

Set `INFERRAIL_RECEIPTS_PATH` to your receipts file. Clients start the
server from their own working directory, so the default
`./inferrail-receipts.jsonl` is rarely the right place. For
`serve --app-mode`, point it at `receipts.db` in Inferrail's data
directory (`~/.local/share/inferrail` on Linux,
`~/Library/Application Support/inferrail` on macOS, `%APPDATA%\inferrail`
on Windows).

Then ask, for example: *"How much did work contract-review-42 cost?"* If
your requests carried `work_id=contract-review-42`, the agent calls
`get_spend` with `by: "work_id"` and reads that group. Full tool
contract: [inferrail-mcp/README.md](../inferrail-mcp/README.md).
