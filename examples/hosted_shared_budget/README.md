# Many agents, one run budget (Inferrail Hosted)

Several independent processes use the standard OpenAI SDK against one Inferrail Hosted
workspace. They work on the same run (one work id) under one dollar ceiling, all at once.

What it shows:
- **The ceiling holds under concurrency.** Combined spend never crosses it. Each call is
  reserved against the run before it's sent, and a call that would cross the ceiling gets a
  402 before it reaches the provider.
- **It holds across processes.** If each agent ran its own local gateway, each would have its
  own ledger and would allow the full ceiling. That's the gap a shared workspace closes.
- **It survives a restart.** The run's spend and receipts are durable, and the run stays
  closed.
- **No rewrite needed.** The agents are plain OpenAI SDK clients. The only changes are the base
  URL, the workspace key as `api_key`, and three headers.

## Run it locally (no keys, no cost)

```bash
pip install -e ".[ap]" -r hosted/budget_gateway/requirements.txt
python examples/hosted_shared_budget/shared_budget.py
```

It starts Inferrail Hosted on your machine with a stand-in for OpenAI that answers instantly
and reports the full `max_tokens` as used. It first runs the agents with per-process caps (each
agent caps only itself, like an in-process cost limit), then on one shared ceiling. Typical
output with `--agents 4 --calls-per-agent 12 --budget 0.002`:

```
  Each agent stayed under its own cap, but together they spent $0.005904: 3.0x the $0.002 you meant to allow.

With one shared ceiling on Inferrail Hosted:

    agent-1:   7 answered,   5 refused (402)
    ...
  16 calls answered, 32 refused before the provider, in 7.9s.
  Run spend recorded by Inferrail: $0.001968 of $0.002 (within the ceiling).
```

With the defaults (6 agents, 25 calls each, $0.005):

```
    agent-1:   7 answered,  18 refused (402)
    ...
    agent-6:   7 answered,  18 refused (402)

  40 calls answered, 110 refused before the provider, in 12.8s.
  Run spend recorded by Inferrail: $0.004920 of $0.005 (within the ceiling).

  Restarted the service on the same data directory: the run still shows $0.004920 spent over
  150 receipts, and a new call is refused.
```

The exact split between agents varies from run to run; the total never exceeds the ceiling.

## Run it against a Hosted deployment

```bash
python examples/hosted_shared_budget/shared_budget.py \
    --base-url https://inferrail-cost-gateway.onrender.com/hosted \
    --provider-key sk-... --budget 0.02
```

It creates a free workspace (250 governed runs a month are free; this run uses one). Your
OpenAI key goes in `X-Provider-Api-Key` on each request. Inferrail never stores it, and OpenAI
bills you for the calls that are answered. The run ceiling (`--budget`) caps that spend. The
restart step only runs in local mode. Add `--compare` to also run the per-process-caps baseline
against the real API; that spends up to agents x budget of your OpenAI usage.

Options: `--agents N`, `--calls-per-agent N`, `--budget USD`, `--compare` / `--no-compare`.
