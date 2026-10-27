# Inferrail Web Read

Read one public web page as clean markdown, paid per page with x402. Built for agents that
need a page's text to finish a task: research, citation checks, docs lookups, extraction.

- **Request:** `POST /read/v1/page` with `{"url": "https://...", "request_id": "optional"}`.
- **Result:** `title`, `content_markdown` (main text, headings, lists, links; navigation,
  footers, scripts and cookie banners removed), `word_count`, `content_sha256`, `final_url`,
  `fetched_at` and a `receipt` (`payment_ref`, `price_usd`, your `request_id`).
- **Price:** $0.002 per delivered page, USDC on Base (`eip155:8453`), x402 `exact`.
- **You pay only for a delivered page.** The payment settles after the page has been fetched
  and converted. Refusals return an error and nothing is charged:
  - 422: `unsupported_url`, `non_public_address`, `dns_failed`, `disallowed_by_robots_txt`,
    `unsupported_content_type`, `too_large` (over 2 MB), `too_many_redirects`;
  - 502: `upstream_status`, `fetch_failed`, `timeout` (15 s).

## How an agent buys a page

1. `POST /read/v1/page` with the JSON body. The response is HTTP 402 with a `PAYMENT-REQUIRED`
   header (x402 v2).
2. Sign it with the agent's wallet and repeat the same request with `PAYMENT-SIGNATURE`.
3. HTTP 200 means delivered and paid. Any other status means not charged.

**A retry never pays twice.** If the response is lost (timeout, dropped connection, crashed
process), send the same `url` and `request_id` again from the same wallet within 10 minutes. Most
x402 clients will sign a new payment when they do; Web Read checks that signature locally, returns
the page you already paid for (with the original `receipt` and `"recovered": true`, header
`x-inferrail-recovered: 1`), and never submits the new payment. A request that arrives while the
first one is still settling waits for it. A different `request_id`, a different URL or a different
wallet is a new read.

**Also payable with MPP on Tempo** when the operator turns it on (`READ_MPP_CHAIN_ID`). The same
402 carries a `WWW-Authenticate: Payment` challenge next to x402's `PAYMENT-REQUIRED`; an MPP
client repeats the request with `Authorization: Payment <credential>`. The signed transfer is
validated first and **broadcast only after the page is delivered**, so the promise is the same
on both rails. (A client that broadcasts the transfer itself and sends only its hash has already
paid; if that page can't be delivered the response says `"refund": "owed"`.)

With AgentCash: `npx agentcash fetch <origin>/read/v1/page -m POST -b '{"url":"https://..."}'`.
The operation is listed in the origin's `/openapi.json` with its price and schemas, and
`GET /read/llms.txt` has the same instructions in plain text.

## How pages are fetched

Directly from the public web, with no third-party data supplier:
- the user agent identifies Inferrail Web Read, and robots.txt is honored (a 5xx or unreachable
  robots.txt means disallowed, per RFC 9309);
- only http(s) on ports 80/443, and every hop must resolve to public addresses (private,
  loopback, link-local and cloud-metadata addresses are refused, and the connected peer is
  re-checked before any body is read);
- no logins, cookies or paywall workarounds; text content types only.

The ledger keeps the URL, sizes, a content hash and the payment reference. The delivered page
itself is kept only for the recovery window (10 minutes by default) so the paying wallet can get
it back; it is deleted when the next request arrives after the window ends.

## Running it (operators)

It mounts at `/read` on the shared host (`hosted/budget_gateway/cohost.py`) only when
`READ_PAY_TO` is set. A configuration error is logged, Web Read stays unmounted, and Hosted
and the trial keep serving.

| Variable | Meaning |
|---|---|
| `READ_PAY_TO` | Receiving address. Must differ from Hosted's (`BG_X402_PAY_TO`) and Search's (`SEARCH_PAY_TO`). |
| `READ_NETWORK` | Defaults to Hosted's network. `eip155:8453` also needs `READ_MAINNET_APPROVED=1`. |
| `READ_PRICE_USD` | Default `0.002`; allowed range 0.001–1. |
| `READ_DATA_DIR` | Default: `web-read` next to `BG_DATA_DIR` (the persistent disk). |
| `READ_MPP_CHAIN_ID` | Unset (MPP off) by default. `42431` = Tempo testnet, `4217` = Tempo mainnet (also needs `READ_MPP_MAINNET_APPROVED=1`). Payments go to `READ_PAY_TO` on Tempo. |
| `READ_RECOVERY_S` | Default `600`; 0–3600. How long a paid page can be recovered without paying again. `0` turns recovery off and keeps no page content. |

The facilitator is the same CDP configuration Hosted uses. The pre-deploy check
(`preflight.py --expect-read-pay-to <address>`) builds Web Read and stops the deploy if it
wouldn't mount. `GET /read/v1/operator/stats` (with `BG_OPERATOR_TOKEN`) reports settled reads,
distinct and repeat payers, revenue, refusals by reason, and a `funnel`: unsigned 402s (price
checks), requests carrying a payment, rejected payments, recoveries and settled reads, each split
by client family taken from the User-Agent (for example `x402scan`, `agentcash`, `python`). No
IPs, URLs or bodies are recorded in it.
