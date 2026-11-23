# @inferrail/ai-sdk-tools

Pay-per-call tools for the [Vercel AI SDK](https://ai-sdk.dev). Your agent can read web
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

## Install

```bash
npm i @inferrail/ai-sdk-tools ai zod @x402/fetch @x402/evm viem
```

`ai` (5 or later) and `zod` (3.25 or later) are peer dependencies; the package has no
dependencies of its own and no install scripts.

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

The wallet pays USDC on Base. Fund it with a few cents; a dollar buys 500 calls. This
package never sees your key: it gets only the fetch you pass. With a plain `fetch`, every
call returns a 402 result and nothing is bought.

`guardInferrailPrices(client)` adds a before-payment hook to your x402 client. For offers
from Inferrail's origin, it refuses an amount above the listed price and any receiving
address other than that service's own. Offers from other sellers pass through untouched.

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
