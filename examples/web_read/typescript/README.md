# Web Read from Node, with the x402 client you already have

`web-read.ts` adds Inferrail Web Read to an agent that already pays with x402. Bring your own
payment-enabled fetch from `@x402/fetch`. This file never sees a key or a seed. Payments are
signed by your `x402Client`, under your own spend rules.

Web Read: one public web page as clean markdown, $0.002 in USDC on Base per delivered page.
Refused or failing pages are never charged, and a retry with the same `request_id` from the same
wallet within 10 minutes returns the paid page without a second charge.

## Use

Copy `web-read.ts` into your project. It has no dependencies of its own. You need
`@x402/fetch` and a registered scheme (here `@x402/evm`).

```ts
import { x402Client, wrapFetchWithPayment } from "@x402/fetch";
import { registerExactEvmScheme } from "@x402/evm/exact/client";
import { guardWebReadPrice, readPage, PageNotBought } from "./web-read.ts";

const client = new x402Client();
registerExactEvmScheme(client, { signer });   // the signer your agent already uses
guardWebReadPrice(client);                     // refuse a Web Read offer > $0.002 or to another payTo
const paidFetch = wrapFetchWithPayment(fetch, client);

try {
  const page = await readPage(paidFetch, "https://example.com/");
  console.log(page.title, page.word_count, page.receipt);
} catch (err) {
  if (err instanceof PageNotBought) console.log(err.code, "not charged; retryable:", err.retryable);
  else throw err;
}
```

- `readPage` sends a `request_id` (pass your own with `{ requestId }` to tie it to a job). On a
  network failure it resends the same id once, which Web Read answers with the already paid
  page.
- `quote()` returns the unpaid offer (amount, network, payTo) without signing anything.

## As an agent tool

`webReadTool` is a JSON-schema tool definition (`name`, `description`, `parameters`).
`runWebReadTool(paidFetch, args)` runs it and returns either the page or a not-charged refusal
the model can act on:

```ts
import { webReadTool, runWebReadTool } from "./web-read.ts";
// Register webReadTool with your framework's tool or function-calling API, then on a call:
const result = await runWebReadTool(paidFetch, { url });
```

**Tested:** `@x402/core`, `@x402/evm` and `@x402/fetch` 2.28.0, with real EIP-3009 signing
against an in-memory Web Read (`bun test`), and the unpaid live quote. Not tested: specific
agent frameworks. The tool shape is plain JSON Schema, so adapt it to your framework's tool type.

## Test

```bash
bun install && bun test
```
