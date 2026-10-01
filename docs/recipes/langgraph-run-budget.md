# Limit the cost of one LangGraph run

A LangGraph agent can loop, retry a failing tool, or call several tools in
parallel. `recursion_limit` bounds how many steps a run takes, not how
many dollars it spends. This puts a dollar budget on one run, enforced
outside the graph: once the run's budget can't cover the next model
call, that call gets HTTP 402 and never reaches the provider.

## Start Inferrail

```bash
pip install inferrail
export OPENAI_API_KEY=sk-...
```

```python
import inferrail

base_url = inferrail.start()   # the gateway, on a background thread in this process
```

No config file and no second terminal: it reads the provider key from
your environment and keeps receipts and budgets in your user data
directory. To run Inferrail as its own process instead, see
[the recipe](agent-run-budget.md#1-start-inferrail-with-budgets-on).

## Give the run a budget

Build the run's model with its id and budget as headers. Every model call
in that run shares the budget, including parallel tool calls:

```python
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from openai import APIStatusError

def agent_for_run(run_id: str, budget_usd: str):
    model = ChatOpenAI(
        model="gpt-4o-mini", base_url=base_url,
        api_key="unused", max_tokens=200,
        default_headers={
            "X-Inferrail-Attribute-Work-Id": run_id,   # every call of this run
            "X-Inferrail-Budget-Usd": budget_usd,      # created on first use
        },
    )
    return create_agent(model, tools=[...])

try:
    agent_for_run("run-7f3a", "0.50").invoke(
        {"messages": [{"role": "user", "content": "..."}]})
except APIStatusError as e:
    if e.status_code != 402:
        raise
    # the run reached its budget; stop or return a fallback
```

`langgraph.prebuilt.create_react_agent` works the same way. Tested with
`langchain` 1.4.3, `langchain-openai` 1.6.7, `langgraph` 1.2.12 and `inferrail` 0.4.8,
two concurrent runs, through both `inferrail serve` and `inferrail.start()`.

## Read the run's cost

```bash
inferrail work run-7f3a
```

Refused calls show up as receipts with no cost; nothing was billed for
them.

## Limits

Each call reserves an estimate (prompt size plus `max_tokens` at list
price), so set `max_tokens`. If a call's real cost exceeds its
reservation it still completes, the overrun is recorded, and the run's
later calls are refused. Only calls that go through Inferrail are
counted. Full details: [Give one AI agent run a dollar budget](agent-run-budget.md#exact-limitations).
