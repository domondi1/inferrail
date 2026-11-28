# Inferrail Web Read for Dify

Read public web pages as clean Markdown inside Dify workflows, chatflows and agents: the page's
full text (not a summary) with its title, published date, authors, language, canonical URL and
a content hash, so answers can quote and date their sources and a re-check can tell whether a
page changed.

**Price:** $0.002 per delivered page, paid from prepaid Inferrail credits. A page that can't be
read (blocked, missing, bot check, robots.txt, JavaScript-only) costs nothing.

## Tools

| Tool | What it does |
|---|---|
| `read_page` | One URL as Markdown plus metadata. Optional `request_id`: a retry with the same URL and id returns the paid page again without a second charge. |
| `read_pages` | Up to 10 URLs (newline- or comma-separated). Each delivered page is charged; pages that can't be read are listed and cost nothing. |

## Get an API key

**Quickest:** open https://inferrail-cost-gateway.onrender.com/hosted/v1/start in a browser,
click **Create my key**, copy the key and pay by card ($10 = 10,000 credits = 5,000 pages).
Then paste the key into the plugin's authorization in Dify. Or with `curl`:

1. **Create a free key:**
   `curl -X POST https://inferrail-cost-gateway.onrender.com/hosted/v1/workspaces`
   returns `{"api_key": "irw_..."}`. It is shown once, so keep it.
2. **Fund it once by card.**
   `curl -X POST "https://inferrail-cost-gateway.onrender.com/hosted/v1/credits/checkout?packs=1" -H "Authorization: Bearer irw_..."`
   returns a Stripe Checkout link. A person opens it and pays: **$10 for 10,000 credits, which is
   5,000 pages**. Card top-ups are a human step. Credits can also be bought with USDC at
   `POST /hosted/v1/credits/x402`.
3. **In Dify:** install the plugin, open its authorization, and paste the key. Dify checks it
   (free) and your workflows spend the balance from then on.

Check the balance with `GET https://inferrail-cost-gateway.onrender.com/hosted/v1/workspace`
(same `Authorization` header). When credits run out, the tools answer "Not enough Inferrail
credits" with the top-up link, and nothing is read or charged.

## Connection requirements

The plugin calls only `https://inferrail-cost-gateway.onrender.com` over HTTPS. Inferrail
fetches the page you ask for on its own servers, from public addresses only, respecting
robots.txt.

## Source, support and contact

- Source: https://github.com/domondi1/inferrail/tree/main/integrations/dify/inferrail_web_read
- Service docs: https://github.com/domondi1/inferrail/tree/main/packages/ai-sdk-tools#pay-with-an-api-key-prepaid-credits-no-wallet
- Issues and contact: https://github.com/domondi1/inferrail/issues
