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
and reports the full `max_tokens` as used. Typical output:

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
    --base-url https://<host> --provider-key sk-... --budget 0.02
```

Your OpenAI key goes in `X-Provider-Api-Key` on each request. Inferrail never stores it, and
OpenAI bills you for the calls that are answered. The run ceiling (`--budget`) caps that spend.
The restart step only runs in local mode.

Options: `--agents N`, `--calls-per-agent N`, `--budget USD`.
