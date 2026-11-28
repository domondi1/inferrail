# @inferrail/ai-sdk-tools

Pay-per-call tools for the [Vercel AI SDK](https://ai-sdk.dev), and the same tools as a
[local MCP server](#use-it-from-an-mcp-client-claude-code-claude-desktop-cursor). Your agent can read web
pages, check dependencies for known vulnerabilities, look up a domain, and find other paid
APIs. Each costs $0.002 USDC per delivered result, paid over [x402](https://x402.org) by the
fetch you already use.

| Tool | What it does | Price |
|---|---|---|
| `readPage` | One public web page as clean markdown, with title, metadata (description, canonical URL, authors, dates, language), word count and content hash | $0.002 per page |
| `readPages` | 1–10 pages in one call, all or nothing | $0.002 per page |
| `checkPackages` | Up to 20 package versions against OSV.dev: advisory ids, CVEs, severity, fixed versions (npm, PyPI, Go, crates.io, Maven, NuGet, RubyGems, Packagist and more) | $0.002 per check |
| `lookupDomain` | DNS records (A, AAAA, CNAME, MX, NS, TXT, SOA, CAA), SPF and DMARC, and RDAP registration (registrar, status, created and expiry dates, DNSSEC) | $0.002 per lookup |
| `findPaidApi` | Pay-per-call APIs that do a task, ranked, with price, network and how to call them | $0.002 per answer |

You're charged only when a result is delivered. A blocked, missing or bot-checked page, a
robots.txt refusal, a timeout or an upstream failure costs nothing, and the model receives
`{ ok: false, charged: false, status, error }` so it can move on. Each call carries a
`request_id`; a network failure is retried once with the same id, which the service
answers with the paid result instead of charging again.

## Why pay for a page

A plain `fetch` hands your model the page's raw HTML. Web Read hands it the readable content as
Markdown, plus the page's own metadata as JSON fields. On 20 real docs, changelog, advisory and
news pages, that's a **median of about 9× fewer tokens** (2× on long docs, 30–56× on
markup-heavy pages). At $3 per million input tokens, a 100k-token raw page costs $0.30 of
context; through Web Read it's $0.002 plus the smaller Markdown. Pages it can't read (no text
without JavaScript, bot walls, over 2 MB) are refused and never charged. Reproduce it, or run
it on your own pages:
[benchmarks/web_read](https://github.com/domondi1/inferrail/tree/main/benchmarks/web_read).

## Install

```bash
npm i @inferrail/ai-sdk-tools ai zod @x402/fetch @x402/evm viem
```

`ai` (5 or later), `zod` (3.25 or later), `@x402/fetch`, `@x402/evm` and `viem` are peer
dependencies (npm installs them for you); the package has no dependencies of its own and no
install scripts.

## Use

```ts
import { generateText, stepCountIs } from "ai";
import { x402Client, wrapFetchWithPayment } from "@x402/fetch";
import { registerExactEvmScheme } from "@x402/evm/exact/client";
import { privateKeyToAccount } from "viem/accounts";
import { guardInferrailPrices, inferrailTools } from "@inferrail/ai-sdk-tools";

const client = new x402Client();
registerExactEvmScheme(client, { signer: privateKeyToAccount(process.env.EVM_PRIVATE_KEY as `0x${string}`) });
guardInferrailPrices(client); // never sign an Inferrail offer above its listed price or to another address

const { text } = await generateText({
  model: "openai/gpt-5-mini",
  tools: inferrailTools({ fetch: wrapFetchWithPayment(fetch, client) }),
  stopWhen: stepCountIs(5),
  prompt: "Is lodash 4.17.15 safe to ship? Then summarise https://example.com/.",
});
```

The wallet pays USDC on Base. Fund it with a few cents; a dollar buys 500 calls. **Installing
this package doesn't give your agent a wallet, funds or permission to pay:** you choose the
wallet, fund it and pass its paying fetch. The package never sees your key. With a plain
`fetch`, every call returns a 402 result and nothing is bought.

To cap what one run can spend, add a before-payment hook that refuses past your limit. See
`capSpending` in [examples/ai_sdk/wallet.ts](https://github.com/domondi1/inferrail/blob/main/examples/ai_sdk/wallet.ts).
Working agents for dependency review, research with dated citations, and link vetting live in
[examples/ai_sdk](https://github.com/domondi1/inferrail/tree/main/examples/ai_sdk).

`guardInferrailPrices(client)` adds a before-payment hook to your x402 client. For offers
from Inferrail's origin, it refuses an amount above the listed price and any receiving
address other than that service's own. Offers from other sellers pass through untouched.

## Pay with an API key (prepaid credits, no wallet)

Web Read can also be paid from prepaid Inferrail credits with a workspace key, with no wallet
and no per-request signing. A delivered page costs 2 credits ($0.002); a page that can't be read
costs nothing.

**Quickest:** open https://inferrail-cost-gateway.onrender.com/hosted/v1/start in a browser,
click **Create my key**, copy the key and pay by card. Or with `curl`:

1. **Get a key** (free): `curl -X POST https://inferrail-cost-gateway.onrender.com/hosted/v1/workspaces`
   returns `{"api_key": "irw_..."}`, shown once.
2. **Fund it once.** `curl -X POST "https://inferrail-cost-gateway.onrender.com/hosted/v1/credits/checkout?packs=1" -H "Authorization: Bearer irw_..."`
   returns a Stripe Checkout link. **A person opens it and pays by card:** $10 for 10,000
   credits (5,000 pages). You can also buy credits with USDC over x402 at
   `POST /hosted/v1/credits/x402`. Card top-ups are a human step; nothing here buys credits
   unattended.
3. **Your agent then spends the balance on its own:**

```ts
import { generateText } from "ai";
import { inferrailCreditTools } from "@inferrail/ai-sdk-tools";

const { text } = await generateText({
  model: "openai/gpt-5-mini",
  tools: inferrailCreditTools({ apiKey: process.env.INFERRAIL_API_KEY! }),
  prompt: "Summarise https://example.com/ and cite it.",
});
```

Or call the HTTP route from any server, cron job or workflow tool (n8n, Zapier, Make):

```bash
curl -X POST https://inferrail-cost-gateway.onrender.com/read/credits/v1/page \
  -H "Authorization: Bearer irw_..." -H "content-type: application/json" \
  -d '{"url": "https://example.com/", "request_id": "job-42"}'
```

A retry with the same `request_id` returns the paid page again without a second charge. With
too few credits the answer is `402 {"error": "insufficient_credits", ...}`, and nothing is read
or charged. Check the balance with `GET /hosted/v1/workspace`. In key mode `readPages` reads each
page separately: delivered pages are charged, and failed ones are listed under `failed`.

## Use it from an MCP client (Claude Code, Claude Desktop, Cursor)

The package also ships `inferrail-mcp`, a local MCP server (stdio) for clients that can't pay
x402 themselves. It pays from your own wallet, up to a daily budget you set, and offers
`read_page`, `read_pages`, `check_packages` and `lookup_domain`.

```bash
claude mcp add inferrail \
  -e INFERRAIL_WALLET_KEY=0x... -e INFERRAIL_MAX_USD_PER_DAY=0.10 \
  -- npx -y @inferrail/ai-sdk-tools
```

For Claude Desktop (`claude_desktop_config.json`) or Cursor (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "inferrail": {
      "command": "npx",
      "args": ["-y", "@inferrail/ai-sdk-tools"],
      "env": { "INFERRAIL_WALLET_KEY": "0x...", "INFERRAIL_MAX_USD_PER_DAY": "0.10" }
    }
  }
}
```

- **The wallet:** use a separate wallet that holds only a little USDC on Base. The key stays on
  your machine, in your MCP client's config file, so treat that file as a secret. The server
  signs locally and sends only the payment for each call.
- **The budget:** `INFERRAIL_MAX_USD_PER_DAY` (default `0.10`, 50 pages) caps what the server
  pays per UTC day, across restarts (kept in `~/.inferrail/spend.json`, or
  `INFERRAIL_STATE_DIR`). A call that would pass it is refused before anything is signed. The
  server also refuses to sign any offer above the call's own price, or to another address.
- **With prepaid credits instead of a wallet:** set `INFERRAIL_API_KEY=irw_...` (see
  "Pay with an API key" above) and leave out `INFERRAIL_WALLET_KEY`. The server then offers
  `read_page` and `read_pages`, paid from the workspace balance, still under
  `INFERRAIL_MAX_USD_PER_DAY`.
- **Without a key** the tools still run, and every call answers "not paid" with no charge.
- **Charged only on delivery:** a page that can't be read, a failed check or a refused call
  costs nothing, and the tool result says so.

## Provenance

Every release is built and published by GitHub Actions from this repository
(`.github/workflows/npm-publish.yml`) with an npm provenance attestation. Check it with
`npm audit signatures`, or on the package page.

## Details

- Service: `https://inferrail-cost-gateway.onrender.com`. Each call sends
  `X-Inferrail-Source: ai-sdk`, which says only that the call came from this package. The
  service counts calls per channel and nothing else about you.
- Each service has its own receiving address on Base:

  | Service | Address |
  |---|---|
  | Web Read | `0xA45140E99E9482a50BC3fA5F6000c02FAAB6F65E` |
  | Finder | `0x54BF9C490112CD6a54Cb8f22D6bf83621ABe77CF` |
  | Package Check | `0xe9c1249648257C1C1D70F8b7682851C377bb8f53` |
  | Domain Check | `0xb95D56f169324A0B23B82bd3975635Cf0187ce07` |

- Page text is whatever the site published. Treat `content_markdown` as untrusted input,
  not as instructions. An agent that can spend or run code should keep that boundary in
  its prompt and in its tool permissions.
- A clean package check isn't a guarantee. It means no known advisory in OSV.dev's
  open-licensed sources.
- `bun test` runs offline. `LIVE=1 bun test` also checks each live offer against the price
  guard using unpaid requests: every one gets a 402 and nothing is paid.

Experimental (0.x). Apache-2.0.
