# Give one AI agent run a dollar budget

An agent run rarely makes one model call. One user request becomes a
plan, then several tool calls, often in parallel, then a summary. This
recipe puts a dollar ceiling on that **whole run**, enforced outside the
agent, and gives you the run's final cost afterwards.

What you get:

- **One run id** tags every call of the run.
- **One budget declaration** per run, sent as a header. There's no budget
  object to create first.
- **Parallel calls share the budget safely.** Each call reserves its
  estimated cost atomically before it's sent, so concurrent calls can't
  all spend the same remaining dollars.
- **Refusal before the provider.** Once the run's remaining budget can't
  cover a call's reservation, that call gets HTTP 402 and never reaches
  the model provider.
- **A final run cost** from the payload-free receipts. Prompts and
  responses are never stored.

## 1. Start Inferrail with budgets on

```bash
pip install inferrail
export OPENAI_API_KEY=sk-...
```

In your app:

```python
import inferrail

base_url = inferrail.start()   # e.g. http://127.0.0.1:53721/v1
```

`inferrail.start()` runs the Inferrail gateway on a background thread in
your process, on a free local port, with budgets on and no config file.
It reads the provider key from your environment. Receipts and budgets
go to SQLite files in your user data directory, so `inferrail work`
finds them later. Calling it again returns the same URL;
`inferrail.stop()` shuts it down (it also stops when your process
exits).

**Or run it as its own process**, for example one gateway shared by
several services:

`inferrail.yaml`:

```yaml
providers:
  openai: {type: openai, api_key_env: OPENAI_API_KEY}
default_provider: openai          # each request's model id is passed through
receipts: {sink: sqlite, path: ./receipts.db}
budgets: {enabled: true, path: ./budgets.db}
```

```bash
inferrail serve --config inferrail.yaml     # http://127.0.0.1:8000/v1
```

Budgets and admission work the same either way. The snippets below use
`base_url`; with `inferrail serve` it's `http://127.0.0.1:8000/v1`.

## 2. Mark the run and declare its budget

Point your OpenAI client at Inferrail and send two headers on every call
of the run:

```python
from openai import AsyncOpenAI

run_id = "run-7f3a"                       # your run / job / request id
client = AsyncOpenAI(
    base_url=base_url,                    # from inferrail.start(), or your gateway's URL
    api_key="unused",                     # the provider key lives in the gateway
    default_headers={
        "X-Inferrail-Attribute-Work-Id": run_id,   # which run this call belongs to
        "X-Inferrail-Budget-Usd": "0.05",          # the run's dollar budget
    },
)
```

The first request of a run creates its budget. Every later request of
the same run must declare the same amount (retries do this naturally).

## 3. Fan out, and see the refusal

```python
import asyncio
from openai import APIStatusError

async def one_call(i):
    try:
        await client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=200,
            messages=[{"role": "user", "content": f"Summarize item {i}."}],
        )
        return "answered"
    except APIStatusError as e:
        if e.status_code == 402:          # refused before the provider
            return "refused"
        raise

outcomes = await asyncio.gather(*(one_call(i) for i in range(8)))
```

When the run's remaining budget can't cover the next call's
reservation, that call gets a 402 like:

```json
{"error": {"code": "INFERRAIL_E010",
           "message": "budget 'work_id:run-7f3a:per_work' ... would be exceeded ...",
           "details": {"budget_id": "work_id:run-7f3a:per_work", "limit_usd": "0.05",
                       "spent_so_far_usd": "...", "reserved_usd": "...", "...": "..."}}}
```

A complete, runnable version is in
[`examples/agent_run_budget.py`](../../examples/agent_run_budget.py). It
is exercised in CI against the gateway: 8 parallel calls on a budget with
room for 4 → 4 answered, 4 refused, 4 provider calls.

## 4. Read the run's final cost

```bash
inferrail work run-7f3a                     # with inferrail serve: add --config inferrail.yaml
```

```text
Work:                              run-7f3a
Inference receipts:                8
Known attributed inference cost:   $0.000...
Unknown-cost inference receipts:   0
```

Refused calls appear as receipts with no cost. Nothing was billed for
them.

## Framework snippets

Every framework below talks to Inferrail through its normal OpenAI-compatible
client. Short standalone pages: [LangGraph](langgraph-run-budget.md),
[OpenAI Agents SDK](openai-agents-sdk-run-budget.md),
[CrewAI](crewai-run-budget.md), [Strands Agents](strands-agents-run-budget.md),
[Vercel AI SDK](vercel-ai-sdk-run-budget.md). What changes is only where the run id and budget go. Each
snippet uses the `base_url` from step 1. Each was run with two concurrent
runs, both through `inferrail serve` (0.4.8) and through
`inferrail.start()` (the TypeScript one only through `inferrail serve`):
the runs stayed separate, and the run with the small budget got a 402.

`gpt-4o-mini` in the snippets is only an example. Inferrail doesn't choose
a model: send any model id your account can use, and it's passed to the
provider as-is. A dollar budget needs a price for that model.
`inferrail models` lists your provider's models and which ones have a
price. For a model Inferrail has no price for yet, pass one yourself:

```python
base_url = inferrail.start(pricing={"openai": {"<model id>": {
    "input_usd_per_million": "...", "output_usd_per_million": "...",
    "source": "OpenAI pricing page", "verified_date": "2026-10-01",
}}})
```

Without a price, that model still works, but a run with a budget refuses
it (`INFERRAIL_E012`) rather than guess what it costs.

### LangChain

One model object; pass the run's headers on each call:

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(model="gpt-4o-mini", base_url=base_url,
                 api_key="unused", max_tokens=200)

llm.invoke("Summarize the ticket.", extra_headers={
    "X-Inferrail-Attribute-Work-Id": "run-7f3a",
    "X-Inferrail-Budget-Usd": "0.50",
})
```

Tested with `langchain-openai` 1.6.7.

### LangChain agents and LangGraph

An agent makes several model calls per run, so give each run its own
model with the headers set once:

```python
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

def agent_for_run(run_id: str, budget_usd: str):
    model = ChatOpenAI(
        model="gpt-4o-mini", base_url=base_url,
        api_key="unused", max_tokens=200,
        default_headers={
            "X-Inferrail-Attribute-Work-Id": run_id,
            "X-Inferrail-Budget-Usd": budget_usd,
        },
    )
    return create_agent(model, tools=[...])

agent_for_run("run-7f3a", "0.50").invoke(
    {"messages": [{"role": "user", "content": "..."}]})
```

A refused call raises `openai.APIStatusError` with `status_code == 402`.
Tested with `langchain` 1.4.3 and `langgraph` 1.2.12
(`create_react_agent` behaves the same).

### OpenAI Agents SDK

One client for the whole process; the run's headers go in its
`RunConfig`, so concurrent runs don't share a budget by accident:

```python
from agents import Agent, ModelSettings, RunConfig, Runner, set_default_openai_api, set_default_openai_client
from openai import AsyncOpenAI

set_default_openai_api("chat_completions")
set_default_openai_client(AsyncOpenAI(base_url=base_url, api_key="unused"))

agent = Agent(name="support", instructions="...", model="gpt-4o-mini",
              model_settings=ModelSettings(max_tokens=200))

await Runner.run(agent, "...", run_config=RunConfig(model_settings=ModelSettings(extra_headers={
    "X-Inferrail-Attribute-Work-Id": "run-7f3a",
    "X-Inferrail-Budget-Usd": "0.50",
})))
```

Tested with `openai-agents` 0.22.3 (Chat Completions API; the Responses
API isn't supported by Inferrail yet).

### Haystack

`OpenAIChatGenerator` with `api_base_url` pointing at Inferrail, and the
run's headers in `generation_kwargs`:

```python
pipeline.run({
    "llm": {"generation_kwargs": {"max_tokens": 300, "extra_headers": {
        "X-Inferrail-Attribute-Work-Id": "support-ticket-4812",
        "X-Inferrail-Budget-Usd": "0.05",
    }}},
    # ... your other components' inputs
})
```

A refused call raises `openai.APIStatusError` (`status_code == 402`) from
the generator; inside `pipeline.run(...)` Haystack wraps it in
`PipelineRuntimeError`, with the 402 as its `__cause__`. Tested with
`haystack-ai` 3.2.0.

### CrewAI

Give each crew run its own `LLM` with the run's headers. Every agent in
the crew that uses it shares the run's budget, including delegated work:

```python
from crewai import LLM

llm = LLM(model="gpt-4o-mini", base_url=base_url,
          api_key="unused", max_tokens=200,
          extra_headers={
              "X-Inferrail-Attribute-Work-Id": "crew-run-7f3a",
              "X-Inferrail-Budget-Usd": "0.50",
          })
# pass llm=llm to each Agent in this crew run
```

CrewAI retries a failed call up to its retry limit; each retry of a
refused call is also refused before the provider, so it costs nothing.
Use `inferrail` 0.4.10 or later in the same environment as CrewAI
(earlier versions required an `mcp` version CrewAI can't install).
Tested with `crewai` 1.15.23 and `inferrail` 0.4.10.

### LlamaIndex

```python
from llama_index.llms.openai_like import OpenAILike

llm = OpenAILike(model="gpt-4o-mini", api_base=base_url,
                 api_key="unused", is_chat_model=True, max_tokens=200,
                 default_headers={
                     "X-Inferrail-Attribute-Work-Id": "run-7f3a",
                     "X-Inferrail-Budget-Usd": "0.50",
                 })
```

Create one per run. Tested with `llama-index-llms-openai-like` 0.8.0.

### Microsoft Agent Framework

Use the Chat Completions client (Inferrail doesn't serve the Responses
API yet), one per run:

```python
from agent_framework import Agent
from agent_framework.openai import OpenAIChatCompletionClient

client = OpenAIChatCompletionClient(
    model="gpt-4o-mini", base_url=base_url, api_key="unused",
    default_headers={
        "X-Inferrail-Attribute-Work-Id": "run-7f3a",
        "X-Inferrail-Budget-Usd": "0.50",
    })
agent = Agent(client=client, instructions="...", tools=[...],
              default_options={"max_tokens": 200})
```

A refused call surfaces as `ChatClientException` wrapping the 402.
Tested with `agent-framework-core` 1.19.0 and `agent-framework-openai`
1.14.4.

### Strands Agents

`OpenAIModel` passes `client_args` to the OpenAI client, so the run's
headers go in `default_headers`. Build one model per run:

```python
from strands import Agent
from strands.models.openai import OpenAIModel

def agent_for_run(run_id: str, budget_usd: str):
    model = OpenAIModel(
        client_args={
            "base_url": base_url,
            "api_key": "unused",
            "default_headers": {
                "X-Inferrail-Attribute-Work-Id": run_id,
                "X-Inferrail-Budget-Usd": budget_usd,
            },
        },
        model_id="gpt-4o-mini",
        params={"max_tokens": 200},
    )
    return Agent(model=model, tools=[...])

agent_for_run("run-7f3a", "0.50")("...")
```

A refused call ends the agent loop with an `EventLoopException` wrapping
the 402. Tested with `strands-agents` 1.57.1.

### Google ADK

Use ADK's `LiteLlm` model with `api_base` pointing at Inferrail and the
run's headers in `extra_headers`, one per run:

```python
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

def agent_for_run(run_id: str, budget_usd: str):
    model = LiteLlm(
        model="openai/gpt-4o-mini", api_base=base_url,
        api_key="unused", max_tokens=200,
        extra_headers={
            "X-Inferrail-Attribute-Work-Id": run_id,
            "X-Inferrail-Budget-Usd": budget_usd,
        },
    )
    return LlmAgent(name="support", model=model, instruction="...", tools=[...])
```

In ADK 2.x a refused call arrives as an event with `error_code` set
rather than an exception. Stop reading events at that one: the runner
raises if you keep iterating. Tested with
`google-adk` 2.10.0 (and 1.10.0) and `litellm` 1.103.2. Only Chat
Completions models routed through `LiteLlm` are covered, not native
Gemini.

### smolagents

`OpenAIServerModel` forwards `client_kwargs` to the OpenAI client:

```python
from smolagents import OpenAIServerModel, ToolCallingAgent

model = OpenAIServerModel(
    model_id="gpt-4o-mini", api_base=base_url,
    api_key="unused", max_tokens=200,
    client_kwargs={"default_headers": {
        "X-Inferrail-Attribute-Work-Id": "run-7f3a",
        "X-Inferrail-Budget-Usd": "0.50",
    }},
)
agent = ToolCallingAgent(tools=[...], model=model)
```

Create one model per run. A refused call raises `AgentGenerationError`
wrapping the 402. Tested with `smolagents` 1.26.0.

### DSPy

`dspy.LM` passes `extra_headers` through to the request. Scope the LM to
one program run with `dspy.context`:

```python
import dspy

lm = dspy.LM("openai/gpt-4o-mini", api_base=base_url,
             api_key="unused", max_tokens=200, cache=False,
             extra_headers={
                 "X-Inferrail-Attribute-Work-Id": "run-7f3a",
                 "X-Inferrail-Budget-Usd": "0.50",
             })

with dspy.context(lm=lm):
    program(question="...")
```

A refused call raises `LMBillingError`. Turn off DSPy's cache
(`cache=False`) if you want every call to reach the gateway and count.
Tested with `dspy` 3.4.0.

### Agno

```python
from agno.agent import Agent
from agno.models.openai.like import OpenAILike

model = OpenAILike(id="gpt-4o-mini", base_url=base_url,
                   api_key="unused", max_tokens=200,
                   default_headers={
                       "X-Inferrail-Attribute-Work-Id": "run-7f3a",
                       "X-Inferrail-Budget-Usd": "0.50",
                   })
agent = Agent(model=model, tools=[...])
run = agent.run("...")
```

Create one model per run. Agno doesn't raise on a refused call: the run
comes back with `run.status == RunStatus.error` and the budget message as
its content, so check the status. Tested with `agno` 3.1.0.

### AG2

AG2 1.x takes an `OpenAIConfig` with `default_headers`, one per run:

```python
from ag2 import Agent
from ag2.config.openai.config import OpenAIConfig

def agent_for_run(run_id: str, budget_usd: str) -> Agent:
    config = OpenAIConfig(
        model="gpt-4o-mini", base_url=base_url, api_key="unused", max_tokens=200,
        default_headers={
            "X-Inferrail-Attribute-Work-Id": run_id,
            "X-Inferrail-Budget-Usd": budget_usd,
        },
    )
    return Agent("support", "...", config=config)

reply = await agent_for_run("run-7f3a", "0.50").ask("...")
```

A refused call raises `openai.APIStatusError` (`status_code == 402`).
Tested with `ag2` 1.1.1.

### AgentScope

```python
from agentscope.credential import OpenAICredential
from agentscope.model import OpenAIChatModel

model = OpenAIChatModel(
    credential=OpenAICredential(api_key="unused", base_url=base_url),
    model="gpt-4o-mini",
    parameters=OpenAIChatModel.Parameters(max_tokens=200),
    client_kwargs={"default_headers": {
        "X-Inferrail-Attribute-Work-Id": "run-7f3a",
        "X-Inferrail-Budget-Usd": "0.50",
    }},
)
```

Create one model per run. A refused call raises `openai.APIStatusError`
(`status_code == 402`). Needs `inferrail` 0.4.10 or later in the same
environment. Tested with `agentscope` 2.0.9.

### CAMEL

```python
from camel.agents import ChatAgent
from camel.models import ModelFactory
from camel.types import ModelPlatformType

model = ModelFactory.create(
    model_platform=ModelPlatformType.OPENAI_COMPATIBLE_MODEL,
    model_type="gpt-4o-mini", url=base_url, api_key="unused",
    model_config_dict={"max_tokens": 200, "extra_headers": {
        "X-Inferrail-Attribute-Work-Id": "run-7f3a",
        "X-Inferrail-Budget-Usd": "0.50",
    }},
)
agent = ChatAgent(model=model)
```

Create one model per run (or per Workforce run). A refused call raises
`openai.APIStatusError` (`status_code == 402`). `camel-ai` 0.2.90 fails to import with `mcp` 2.x, so keep `mcp<2` in
that environment (`inferrail` 0.4.10 or later doesn't require `mcp`).
Tested with `camel-ai` 0.2.90, `mcp` 1.29 and `inferrail` 0.4.10.

### Semantic Kernel (Python)

Give the connector an `AsyncOpenAI` client with the run's headers:

```python
from openai import AsyncOpenAI
from semantic_kernel.connectors.ai.open_ai import OpenAIChatCompletion

service = OpenAIChatCompletion(
    ai_model_id="gpt-4o-mini",
    async_client=AsyncOpenAI(base_url=base_url, api_key="unused", default_headers={
        "X-Inferrail-Attribute-Work-Id": "run-7f3a",
        "X-Inferrail-Budget-Usd": "0.50",
    }),
)
```

Create one service per run. A refused call raises
`ServiceResponseException` wrapping the 402. Needs `inferrail` 0.4.10 or later in
the same environment. Tested with `semantic-kernel` 1.44.1.

### Langroid

```python
import langroid as lr
import langroid.language_models as lm

llm_config = lm.OpenAIGPTConfig(
    chat_model="gpt-4o-mini", api_base=base_url, api_key="unused",
    max_output_tokens=200,
    headers={
        "X-Inferrail-Attribute-Work-Id": "run-7f3a",
        "X-Inferrail-Budget-Usd": "0.50",
    },
)
agent = lr.ChatAgent(lr.ChatAgentConfig(llm=llm_config))
```

Create one config per run. Langroid retries a refused call several times
before raising; each retry is refused before the provider, so retries
cost nothing. Tested with `langroid` 0.68.2.

### BeeAI Framework

```python
from beeai_framework.adapters.openai import OpenAIChatModel

model = OpenAIChatModel(
    "gpt-4o-mini", base_url=base_url, api_key="unused",
    settings={"extra_headers": {
        "X-Inferrail-Attribute-Work-Id": "run-7f3a",
        "X-Inferrail-Budget-Usd": "0.50",
    }},
)
```

Create one model per run. A refused call raises `ChatModelError`. Tested
with `beeai-framework` 0.1.85.

### Griptape

```python
from griptape.artifacts import ErrorArtifact
from griptape.drivers.prompt.openai import OpenAiChatPromptDriver
from griptape.structures import Agent
from openai import OpenAI

driver = OpenAiChatPromptDriver(
    model="gpt-4o-mini", base_url=base_url, api_key="unused", max_tokens=200,
    client=OpenAI(base_url=base_url, api_key="unused", default_headers={
        "X-Inferrail-Attribute-Work-Id": "run-7f3a",
        "X-Inferrail-Budget-Usd": "0.50",
    }),
)
result = Agent(prompt_driver=driver).run("...")
if isinstance(result.output, ErrorArtifact):
    ...  # the run reached its budget
```

Create one driver per run. Griptape doesn't raise on a refused call: the
task finishes with an `ErrorArtifact` carrying the 402, so check the
output. Tested with `griptape` 1.13.0.

### Atomic Agents

Atomic Agents uses an `instructor` client, so the headers go on the
OpenAI client underneath:

```python
import instructor
from openai import OpenAI
from atomic_agents import AgentConfig, AtomicAgent, BasicChatInputSchema, BasicChatOutputSchema

client = instructor.from_openai(OpenAI(base_url=base_url, api_key="unused", default_headers={
    "X-Inferrail-Attribute-Work-Id": "run-7f3a",
    "X-Inferrail-Budget-Usd": "0.50",
}))
agent = AtomicAgent[BasicChatInputSchema, BasicChatOutputSchema](
    config=AgentConfig(client=client, model="gpt-4o-mini", model_api_parameters={"max_tokens": 200}))
```

Create one client per run. `instructor` retries a failed call; each retry
of a refused call is refused before the provider. A refused run raises
`InstructorRetryException` with the 402 in it. Tested with
`atomic-agents` 2.10.3.

### Vercel AI SDK (TypeScript)

Inferrail is a separate process, so a TypeScript app can use it over
HTTP. Use the OpenAI-compatible provider and pass the run's headers per
call:

```ts
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import { generateText } from 'ai';

const inferrail = createOpenAICompatible({
  name: 'inferrail',
  baseURL: 'http://127.0.0.1:8000/v1',
  apiKey: 'unused',
  includeUsage: true,
});

await generateText({
  model: inferrail('gpt-4o-mini'),
  maxOutputTokens: 200,
  headers: {
    'X-Inferrail-Attribute-Work-Id': 'run-7f3a',
    'X-Inferrail-Budget-Usd': '0.50',
  },
  tools: { /* ... */ },
  prompt: '...',
});
```

Every step of a multi-step `generateText` call carries the same headers.
A refused call throws an error with `statusCode === 402`. Tested with
`ai` 7.0.126 and `@ai-sdk/openai-compatible` 3.0.62. A TypeScript app
can't call `inferrail.start()`, so run the gateway next to it with
`pip install inferrail` and `inferrail serve --quickstart --app-mode`
(no config file; `inferrail work <run-id>` reads its receipts).

### Mastra (TypeScript)

Mastra agents take an AI SDK model, so the OpenAI-compatible provider
works as-is. Keep one agent and pass the run's headers on each call:

```ts
import { Agent } from '@mastra/core/agent';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';

const inferrail = createOpenAICompatible({
  name: 'inferrail', baseURL: 'http://127.0.0.1:8000/v1', apiKey: 'unused', includeUsage: true,
});
const agent = new Agent({
  id: 'support', name: 'support', instructions: '...',
  model: inferrail.chatModel('gpt-4o-mini'), tools: { /* ... */ },
});

await agent.generate('...', { modelSettings: {
  maxOutputTokens: 200,
  headers: { 'X-Inferrail-Attribute-Work-Id': 'run-7f3a', 'X-Inferrail-Budget-Usd': '0.50' },
} });
```

A refused call throws an error with `statusCode === 402`. Run the gateway
next to the app (`inferrail serve --quickstart --app-mode`). Tested with
`@mastra/core` 1.74.0 through `inferrail serve`.

## Using an existing gateway instead of calling the provider directly

Inferrail can sit in front of an OpenAI-compatible gateway you already
run, just for per-run budgets:

```yaml
providers:
  gw:
    type: openai_compatible
    api_key_env: GATEWAY_KEY            # e.g. your LiteLLM virtual key
    base_url: http://127.0.0.1:4000/v1
    price_as: openai                    # you assert the gateway bills at OpenAI list prices
    request_stream_usage: true          # ask the gateway for stream usage
```

This was tested locally in front of LiteLLM and otari. Other gateways
haven't been tested. If that gateway refuses a call because of its own
budget, you get `INFERRAIL_E014` (HTTP 402, not retried). See
[ADR 0022](../adr/0022-per-run-budget-declaration.md).

## Exact limitations

- **Reservations are estimates, not a proven upper bound.** A call
  reserves roughly its prompt size plus `max_tokens` (or
  `max_completion_tokens`) at list price. **Set `max_tokens`**: without
  it, Inferrail assumes 4,096 output tokens, which can refuse calls far
  earlier than their real cost would. If a call's actual cost exceeds its
  reservation, it still completes, the excess is recorded as
  `budget_overrun_usd`, and later calls are refused. This is a ceiling on
  admission, not a guarantee that spend can never pass the limit.
- **Calls that may have been billed but reported no usage are held.** A
  timeout, dropped connection, cancelled stream, or stream without usage
  keeps its reservation counted against the run. It shows on the receipt
  as `budget_held_usd`, never as cost.
- **Unpriced models are refused** under a budget (`INFERRAIL_E012`). Add
  a `pricing:` override to use one.
- **Sub-agents** share the run's budget only by reusing its run id. There
  are no nested budgets.
- **Anyone who can call the gateway can declare budgets.** A declaration
  only ever adds a limit to a run that doesn't have one yet; it can't
  raise an existing one. Use `INFERRAIL_GATEWAY_TOKEN`,
  `budgets.per_work_max_usd`, or `budgets.allow_declared_budgets: false`
  to control this.
- **Each run leaves one small budget row.** Lookups stay fast as they
  accumulate, but there's no automatic cleanup yet.
- **Only traffic through this gateway is counted.** It doesn't see other
  spend on your provider account.

Did this work for you, or not?
[Open an issue](https://github.com/domondi1/inferrail/issues/new?title=agent-run-budget%20recipe%3A%20)
with what happened. It directly shapes what we fix next.
