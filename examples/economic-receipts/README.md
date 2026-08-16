# Economic receipts — offline demo

**DEMO DATA / NOT REAL PROVIDER BILLING.**

This runs six scripted chat "requests" through Inferrail's real
`InferenceEngine` → `InferenceReceipt` → `inferrail report` pipeline, using
a fake in-memory provider instead of a real network call. No API key,
no signup, no cost. It exists to answer one question in under a minute:
*what does an Inferrail economic receipt actually look like, and what does
`inferrail report` do with a pile of them?*

```bash
pip install -e .          # from the repo root, if you haven't already
python examples/economic-receipts/run_demo.py
```

You'll see a table like:

```
CUSTOMER        REQUESTS  INPUT TOKENS  OUTPUT TOKENS  COST (USD)  UNKNOWN COST
globex          2         1621          416            $0.007825
acme            3         1952          361            $0.000483   1
(unattributed)  1         300           50             $0.0001
--------------  --------  ------------  -------------  ----------  ------------
TOTAL           6         3873          827            $0.008408   1
```

That `1` under `UNKNOWN COST` for `acme` is deliberate: one of the six
scripted requests uses a model Inferrail has no price on file for, so its
cost is left `null` rather than guessed at `$0` — the same honest behavior
a real unrecognized model gets in production. See
`examples/economic-receipts/demo-receipts.jsonl` (created after you run
the script) for the full receipts, including that one.

## What's real and what's fake here

- **Fake:** the provider. `run_demo.py` defines a small `DemoProvider`
  that returns scripted text and token counts instead of calling OpenAI.
  It lives only in this script — nothing in `src/inferrail` knows it
  exists, and there is no `inferrail.yaml` setting that reaches it.
- **Fake:** the prices. Both per-token prices used are made-up round
  numbers, and every receipt's `pricing.source` says so explicitly
  (`"DEMO — a made-up round number, not a real provider price"`) — open
  the JSONL file and check for yourself.
- **Real:** everything downstream of the provider call. Routing
  (`Router`), pricing resolution (`PricingResolver`), `Decimal` cost
  arithmetic, receipt assembly, the JSONL sink, and `inferrail report`'s
  aggregation are the exact same code `inferrail serve` uses for a real
  request — see `src/inferrail/gateway/execution.py`.

## Next step

To see this against a real provider — the only way to get a real cost
number instead of a demo one — follow the Quickstart in the top-level
[README.md](../../README.md). It's the same shape: send a request with
`X-Inferrail-Attribute-*` headers, then run `inferrail report --by
customer`.
