---
name: inferrail-web-read
description: Read a public web page as clean markdown (title, main text, headings, lists, links, tables, word count, content hash) plus the page's own metadata (description, canonical URL, authors, published and modified dates, language), and pay $0.002 per delivered page in USDC over x402 on Base or MPP on Tempo, as an MCP tool or an HTTP call. Use when an agent with its own wallet needs the text of a URL, an article, docs page or product page; refused or failing pages are never charged, and a retry never pays twice.
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

Already paying with AgentCash? `npx agentcash add https://inferrail-cost-gateway.onrender.com`
lists Web Read (and the x402 Finder) in your agent's paid tools; it pays from the AgentCash
wallet you already have. With a Coinbase agentic wallet:
`npx awal x402 pay https://inferrail-cost-gateway.onrender.com/read/v1/page -X POST -d '{"url": "https://..."}'`.

## Several pages in one call

When `https://inferrail-cost-gateway.onrender.com/openapi.json` lists `POST /read/v1/pages`,
you can read 1 to 10 pages in one paid request:
`{"urls": ["https://...", "https://..."], "request_id": "<unique id>"}`.

- Use it when a task already has its list of sources (research, citation checks, comparing
  docs); use `/read/v1/page` for one page, or when you decide the next URL from the last one.
- Price: $0.002 per page, one payment for the batch (3 URLs: $0.006; 10 URLs: $0.02). Check that
  the 402's amount is 2000 units (USDC) times the number of URLs before you sign.
- All or nothing: if any page can't be read, nothing is charged and no page is returned. The
  answer (`error: pages_failed`) has `results`, one per URL, with `delivered` and, for each
  failure, `error`. Send the batch again with only the delivered URLs and a new `request_id`.
- A lost response: send the same `urls` in the same order with the same `request_id` from the
  same wallet within 10 minutes; you get the paid batch and are not charged again.
- No duplicate URLs and no more than 10: such a request gets 422 before any payment offer.

## What a delivered page contains

`title`, `content_markdown`, `word_count`, `content_sha256`, `final_url`, and `metadata`:
`description`, `canonical_url`, `authors`, `published_at`, `modified_at` (ISO 8601, as the page
wrote it), `language`, `site_name`, `image` and `type`. Each is exactly what the page declares
(meta tags, Open Graph, JSON-LD); a field the page doesn't declare is `null` (`authors: []`),
never guessed. Cite `canonical_url` and `published_at` when you quote a source.

## Rules that keep you from paying twice

- Always send a `request_id`. If a response is lost, send the same url and request_id
  again from the same wallet within 10 minutes: you get the paid page and the new
  signature isn't settled.
- Refused or failing pages cost nothing: 422 (unsupported or private address,
  robots.txt, non-text, over 2 MB, the site answered 4xx, the site served a bot check
  (`bot_check`), or the page has no text without JavaScript (`no_readable_content`);
  retrying won't help);
  502 or 504 (the site failed or timed out; may work later).
- Guide: https://inferrail-cost-gateway.onrender.com/read/llms.txt. Contract:
  https://inferrail-cost-gateway.onrender.com/openapi.json.
