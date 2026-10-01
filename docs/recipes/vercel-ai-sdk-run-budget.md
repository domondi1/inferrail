# Put a dollar budget on one Vercel AI SDK agent run

A multi-step `generateText` call with tools can make many model calls
for one user request, and a loop or a failing tool makes that number
hard to predict. This puts a dollar budget on one run, enforced outside
your app: once the run's budget can't cover the next model call, that
call gets HTTP 402 and never reaches the provider.

Inferrail is a gateway that runs as its own process, so your app stays
in TypeScript and talks to it over HTTP. The gateway itself is installed
with Python.

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

Use the OpenAI-compatible provider and pass the run's id and budget as
headers on the call:

```ts
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import { generateText, stepCountIs } from 'ai';

const inferrail = createOpenAICompatible({
  name: 'inferrail',
  baseURL: 'http://127.0.0.1:8000/v1',
  apiKey: 'unused', // the provider key lives in the gateway
  includeUsage: true,
});

try {
  const { text } = await generateText({
    model: inferrail('gpt-4o-mini'),
    maxOutputTokens: 200,
    headers: {
      'X-Inferrail-Attribute-Work-Id': 'run-7f3a', // every call of this run
      'X-Inferrail-Budget-Usd': '0.50',            // created on first use
    },
    tools: { /* ... */ },
    stopWhen: stepCountIs(10),
    prompt: '...',
  });
} catch (err: any) {
  if (err?.statusCode !== 402) throw err;
  // the run reached its budget; stop or return a fallback
}
```

Every step of the call carries the same headers, so they all share one
budget. Calls to `generateText` or `streamText` in different requests
can share a run too: send the same run id and the same budget. Tested
with `ai` 7.0.126, `@ai-sdk/openai-compatible` 3.0.62 and `inferrail`
0.4.8, two concurrent runs: the runs stayed separate and the one with the
small budget was refused.

## Read the run's cost

```bash
inferrail work run-7f3a --config inferrail.yaml
```

Refused calls show up as receipts with no cost; nothing was billed for
them.

## Limits

Inferrail serves the Chat Completions API, not the Responses API. Each
call reserves an estimate (prompt size plus `maxOutputTokens` at list
price), so set `maxOutputTokens`. If a call's real cost exceeds its
reservation it still completes, the overrun is recorded, and the run's
later calls are refused. Full details: [Give one AI agent run a dollar budget](agent-run-budget.md#exact-limitations).
