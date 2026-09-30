# Put a dollar limit on one OpenAI Agents SDK run

`max_turns` bounds how many turns a run takes; OpenAI's spend
limits are monthly and set per organization or project. This puts a dollar budget on one
`Runner.run`, enforced outside the agent: once the run's budget can't
cover the next model call, that call gets HTTP 402 and never reaches
OpenAI.

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

Use one client for the process and put the run's id and budget in its
`RunConfig`, so concurrent runs each keep their own budget:

```python
from agents import Agent, ModelSettings, RunConfig, Runner, set_default_openai_api, set_default_openai_client
from openai import APIStatusError, AsyncOpenAI

set_default_openai_api("chat_completions")
set_default_openai_client(AsyncOpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused"))

agent = Agent(name="support", instructions="...", model="gpt-4o-mini",
              model_settings=ModelSettings(max_tokens=200))

def run_config(run_id: str, budget_usd: str) -> RunConfig:
    return RunConfig(model_settings=ModelSettings(extra_headers={
        "X-Inferrail-Attribute-Work-Id": run_id,   # every call of this run
        "X-Inferrail-Budget-Usd": budget_usd,      # created on first use
    }))

try:
    await Runner.run(agent, "...", run_config=run_config("run-7f3a", "0.50"))
except APIStatusError as e:
    if e.status_code != 402:
        raise
    # the run reached its budget
```

Inferrail serves the Chat Completions API, not the Responses API, hence
`set_default_openai_api("chat_completions")`. Tested with
`openai-agents` 0.22.3 and `inferrail` 0.4.8, two concurrent runs.

## Read the run's cost

```bash
inferrail work run-7f3a --config inferrail.yaml
```

Refused calls show up as receipts with no cost; nothing was billed for
them.

## Limits

Each call reserves an estimate (prompt size plus `max_tokens` at list
price), so set `max_tokens`. If a call's real cost exceeds its
reservation it still completes, the overrun is recorded, and the run's
later calls are refused. Only calls that go through Inferrail are
counted. Full details: [Give one AI agent run a dollar budget](agent-run-budget.md#exact-limitations).
