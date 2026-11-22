---
name: inferrail-web-read
description: Read a public web page as clean markdown (title, main text, headings, lists, links, tables, word count, content hash) and pay $0.002 per delivered page in USDC over x402 on Base or MPP on Tempo, as an MCP tool or an HTTP call. Use when an agent with its own wallet needs the text of a URL, an article, docs page or product page; refused or failing pages are never charged, and a retry never pays twice.
---

# Inferrail Web Read

One public web page as clean markdown, $0.002 per delivered page. No account, no API key:
your agent pays per page from its own wallet.

## As an MCP tool

- Server: `https://inferrail-cost-gateway.onrender.com/read/mcp/` (MCP streamable HTTP).
  Official MCP Registry name: `io.github.domondi1/inferrail-web-read`.
- Tool: `read_page(url, request_id?)`.
- Call it without payment: the result is an x402 PaymentRequired. Pay with an x402 MCP client
  (for example the x402 SDK's `x402MCPSession`), which repeats the call with the payment in
  `_meta["x402/payment"]`. The page comes back with the payment response.

## Over HTTP

1. `POST https://inferrail-cost-gateway.onrender.com/read/v1/page` with
   `{"url": "https://...", "request_id": "<unique id>"}`.
2. The answer is HTTP 402. Check the offer before you sign: x402 exact, 2000 units
   ($0.002) of USDC on Base (`eip155:8453`). The same 402 also offers MPP on Tempo.
3. Sign and repeat the request (x402: `PAYMENT-SIGNATURE` header; MPP:
   `Authorization: Payment`). HTTP 200 is the page and its receipt.

A ready, tested buyer: `examples/web_read/buy_page.py` in
https://github.com/domondi1/inferrail. It pays from `EVM_PRIVATE_KEY`, refuses any offer
above `--max-usd` before signing, and retries a lost response safely.

From Node with an x402 client you already use (`@x402/fetch`):
`examples/web_read/typescript/web-read.ts` in the same repository. Pass it your
`wrapFetchWithPayment(fetch, client)`; it never sees a key. `guardWebReadPrice(client)` makes
your client refuse a Web Read offer above $0.002 or to another address before signing, and
`readPage(paidFetch, url)` sends a request_id and retries a lost response with it.

## Rules that keep you from paying twice

- Always send a `request_id`. If a response is lost, send the same url and request_id
  again from the same wallet within 10 minutes: you get the paid page and the new
  signature isn't settled.
- Refused or failing pages cost nothing: 422 (unsupported or private address,
  robots.txt, non-text, over 2 MB, or the site answered 4xx; retrying won't help);
  502 or 504 (the site failed or timed out; may work later).
- Guide: https://inferrail-cost-gateway.onrender.com/read/llms.txt. Contract:
  https://inferrail-cost-gateway.onrender.com/openapi.json.
