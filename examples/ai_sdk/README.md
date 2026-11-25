# Vercel AI SDK agents with Inferrail's paid tools

Four small agents built on [`@inferrail/ai-sdk-tools`](../../packages/ai-sdk-tools/README.md).
Each one does a real job and gets only the tools that job needs.

| Job | Command | Tools | Typical cost |
|---|---|---|---|
| Are these dependency versions safe to ship? | `bun cli.ts deps package.json` | `checkPackages` | $0.002 (up to 20 packages in one check) |
| Answer a question from several pages, with dated citations | `bun cli.ts research "What changed in v5?" <url> <url>...` | `readPages` | $0.002 per page, 1–10 pages |
| Should I trust this link? | `bun cli.ts link https://example.com/` | `lookupDomain`, `readPage` | $0.004 |
| What changed in these docs, changelogs and release notes since the last run? Run it on a schedule. | `bun cli.ts watch urls.txt state.json` (or daily in [GitHub Actions](#run-it-on-a-schedule-github-actions)) | Web Read batch (`watch.ts`) | $0.002 per page per check; only **changed** pages (by `content_sha256`) reach the model |

## What you need

- **A wallet you control with a little USDC on Base.** Put its key in `EVM_PRIVATE_KEY`.
  Installing the package doesn't give your agent a wallet, funds or permission to pay. You
  provide the key, and nothing here creates or funds a wallet.
- **A spending cap.** `MAX_USD` defaults to `0.01` per run. `watch` needs $0.002 × the
  number of pages per check (`MAX_USD=0.04` for 20 pages) and refuses to start below that. Once a payment would take the run
  past it, the payment is refused before it's signed (`wallet.ts`, `capSpending`). The price
  guard also refuses any Inferrail offer above the listed $0.002 per result, or one that pays
  another address.
- **A model.** `MODEL` defaults to `openai/gpt-5-mini` through the Vercel AI Gateway
  (`AI_GATEWAY_API_KEY`). Any AI SDK provider works: pass its model to the functions in
  `tasks.ts`.

```bash
cd examples/ai_sdk && npm install
EVM_PRIVATE_KEY=0x... MAX_USD=0.01 AI_GATEWAY_API_KEY=... bun cli.ts link https://example.com/
```

You're charged only for delivered results. A refused page (robots.txt, bot check, 404), an
unknown domain or a failed check costs nothing. The model receives
`{ ok: false, charged: false, ... }` and carries on.

## Run it on a schedule (GitHub Actions)

[`page-watch.yml`](page-watch.yml) runs `watch` every day and puts the changed pages in the
run's summary. It needs no model key (`MODEL: none` lists the changes instead of summarising
them), and it remembers the last run's page hashes in the Actions cache.

1. Copy this directory into your repository as `.github/page-watch/`, and put the pages to
   watch in `.github/page-watch/urls.txt`, one URL per line.
2. Copy `page-watch.yml` to `.github/workflows/page-watch.yml`.
3. Add a repository secret `PAGE_WATCH_WALLET_KEY`: the key of a wallet that holds only a
   little USDC on Base. Keep this wallet separate and fund it with only what the job should
   spend. Anyone who can change workflows in the repository can use the secret.
4. Optional: a repository variable `PAGE_WATCH_MAX_USD` (default `0.04`, enough for 20 pages
   per run). 20 pages a day costs $0.04 a day, about $1.20 a month.

## Using it in your own agent

```ts
import { inferrailTools } from "@inferrail/ai-sdk-tools";
import { paidFetchFromEnv } from "./wallet";

const { fetch } = paidFetchFromEnv(); // your key, the price guard, your cap
const { readPages, checkPackages } = inferrailTools({ fetch });
// pass { readPages, checkPackages } as `tools` to generateText / streamText
```

Treat page text as untrusted input. The system prompts in `tasks.ts` tell the model so.
That doesn't replace limiting what the agent is allowed to do.

## Tests

`bun test` runs each agent through a real AI SDK `generateText` loop with a scripted model and
a mocked service. It checks the tools offered, the requests made, how refusals reach the model,
and the cap. It makes no network calls and spends nothing.
