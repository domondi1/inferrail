# Inferrail Hosted: the whole customer journey

One script walks through everything a new user or agent does, using the standard **OpenAI and
Anthropic SDKs**. It uses nothing but what the service publishes:

1. read the terms (`GET /pricing`);
2. create a workspace (no account);
3. run a budgeted call with the OpenAI SDK;
4. run a budgeted call with the Anthropic SDK;
5. read each run's receipts and the workspace usage;
6. send a call over its run ceiling: refused with 402 before the provider, and not billed;
7. find what to do when the free allowance runs out.

Each provider step is labelled `[mock provider]` or `[live provider]`, so a stand-in is never
mistaken for a real model.

## Local (no keys, no cost)

```bash
pip install -e ".[ap]" anthropic -r hosted/budget_gateway/requirements.txt
python examples/hosted_quickstart/journey.py
```

It starts Inferrail Hosted on your machine with stand-ins for OpenAI and Anthropic.

## Live

```bash
python examples/hosted_quickstart/journey.py --base-url https://<host> \
    --openai-key sk-... --anthropic-key sk-ant-...
```

Either key alone works too. Your keys go in `X-Provider-Api-Key` on each request and are never
stored. The calls are tiny, and the run ceilings cap them at a cent each.

For many agents sharing one budget, see
[`examples/hosted_shared_budget`](../hosted_shared_budget/).
