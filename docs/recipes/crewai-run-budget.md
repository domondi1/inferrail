# Cap the cost of one CrewAI crew run

`max_iter` and `max_rpm` bound iterations and request rate, not dollars,
and a crew that delegates can have several agents spending at once. This
puts one dollar budget on a whole crew run, shared by every agent in it
and enforced outside the crew: once the run's budget can't cover the
next model call, that call gets HTTP 402 and never reaches the provider.

## Start Inferrail

CrewAI pins an older version of the `mcp` package than Inferrail needs,
so `pip install inferrail` into the same environment as CrewAI fails to
resolve. Run Inferrail from its own environment instead, next to your
app:

```bash
python -m venv ~/.inferrail
~/.inferrail/bin/pip install inferrail
export OPENAI_API_KEY=sk-...                          # only the gateway process sees it
~/.inferrail/bin/inferrail serve --quickstart --app-mode   # http://127.0.0.1:8000/v1
```

No config file: budgets are on and receipts go to your user data
directory. For a custom setup, use an `inferrail.yaml` as in
[the recipe](agent-run-budget.md#1-start-inferrail-with-budgets-on).

## Give the crew run a budget

Create one `LLM` per crew run with the run's id and budget, and give it
to every agent in that run:

```python
from crewai import LLM, Agent, Crew, Task

base_url = "http://127.0.0.1:8000/v1"   # the Inferrail gateway started above

def llm_for_run(run_id: str, budget_usd: str) -> LLM:
    return LLM(model="gpt-4o-mini", base_url=base_url,
               api_key="unused", max_tokens=200,
               extra_headers={
                   "X-Inferrail-Attribute-Work-Id": run_id,   # every call of this run
                   "X-Inferrail-Budget-Usd": budget_usd,      # created on first use
               })

llm = llm_for_run("crew-run-7f3a", "0.50")
researcher = Agent(role="researcher", goal="...", backstory="...", llm=llm)
writer = Agent(role="writer", goal="...", backstory="...", llm=llm)
crew = Crew(agents=[researcher, writer], tasks=[...])
crew.kickoff()
```

When the budget is used up, the next call fails with a 402
(`INFERRAIL_E010`). CrewAI retries a failed call up to its retry limit;
each retry is also refused before the provider, so it costs nothing.
Tested with `crewai` 1.15.23 and `inferrail` 0.4.9, with the gateway
in its own environment as above.

## Read the run's cost

```bash
~/.inferrail/bin/inferrail work crew-run-7f3a
```

Refused calls show up as receipts with no cost; nothing was billed for
them.

## Limits

Each call reserves an estimate (prompt size plus `max_tokens` at list
price), so set `max_tokens`. If a call's real cost exceeds its
reservation it still completes, the overrun is recorded, and the run's
later calls are refused. Only calls that go through Inferrail are
counted. Full details: [Give one AI agent run a dollar budget](agent-run-budget.md#exact-limitations).
