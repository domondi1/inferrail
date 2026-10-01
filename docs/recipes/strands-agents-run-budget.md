# Put a dollar budget on one Strands Agents run

A Strands agent decides for itself how many model calls a task takes. A
failing tool or a long plan can turn one request into dozens of calls.
This puts a dollar budget on one agent run, enforced outside the agent:
once the run's budget can't cover the next model call, that call gets
HTTP 402 and never reaches the provider.

## Start Inferrail

```bash
pip install inferrail
export OPENAI_API_KEY=sk-...        # only the gateway process sees it
```

`inferrail.yaml`:

```yaml
providers:
  openai: {type: openai, api_key_env: OPENAI_API_KEY}
routes:
  default: {provider: openai, model: gpt-4o-mini}
default_provider: openai
receipts: {sink: sqlite, path: ./receipts.db}
budgets: {enabled: true, path: ./budgets.db}
```

```bash
inferrail serve --config inferrail.yaml     # http://127.0.0.1:8000/v1
```

## Give the run a budget

`OpenAIModel` passes `client_args` to the OpenAI client. Build one model
per run with the run's id and budget as headers:

```python
from strands import Agent
from strands.models.openai import OpenAIModel

def agent_for_run(run_id: str, budget_usd: str):
    model = OpenAIModel(
        client_args={
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "unused",                        # the provider key lives in the gateway
            "default_headers": {
                "X-Inferrail-Attribute-Work-Id": run_id,  # every call of this run
                "X-Inferrail-Budget-Usd": budget_usd,     # created on first use
            },
        },
        model_id="gpt-4o-mini",
        params={"max_tokens": 200},
    )
    return Agent(model=model, tools=[...])

agent_for_run("run-7f3a", "0.50")("...")
```

When the run reaches its budget, the agent loop stops with an
`EventLoopException` wrapping the 402. Tested with `strands-agents`
1.57.1 and `inferrail` 0.4.8, two concurrent runs: the runs stayed
separate and the one with the small budget was refused.

## Read the run's cost

```bash
inferrail work run-7f3a --config inferrail.yaml
```

Refused calls show up as receipts with no cost; nothing was billed for
them.

## Limits

This covers Strands' OpenAI (Chat Completions) model provider. Bedrock
and other native providers don't go through Inferrail. Each call
reserves an estimate (prompt size plus `max_tokens` at list price), so
set `max_tokens`. If a call's real cost exceeds its reservation it still
completes, the overrun is recorded, and the run's later calls are
refused. Full details: [Give one AI agent run a dollar budget](agent-run-budget.md#exact-limitations).
