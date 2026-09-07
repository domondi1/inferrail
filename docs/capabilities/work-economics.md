# Inferrail Work Economics — v1

**Status:** Base Sepolia testnet only. Not mainnet, not real money yet.

Inferrail's first paid, hosted capability. Given a caller-declared list of
economic events for one unit of AI work, it returns a normalized cost
summary and a commercial receipt. Payment is the real
[x402](https://www.x402.org/) protocol — any x402-capable agent can buy it
with its own wallet, no Inferrail account required.

## What it does

You describe the cost metadata for a unit of work — one or more "economic
events" (an inference call, a search call, a tool call, anything with a
declared cost) — and Inferrail returns:

- a known total cost, but **only** when every event's cost is known —
  otherwise `known_total_cost_usd` is `null` and `unknown_event_count` says
  how many events it couldn't total
- a breakdown by resource class and by supplier
- where each cost figure came from (`price_provenance`)
- a commercial receipt for the purchase itself, with the identifiers needed
  to reconcile it independently (`purchase_id`, `invocation_id`, `work_id`)

Inferrail never sees or stores prompt/response content here — only the cost
metadata you send it. `EconomicEvent` has no field capable of holding that
kind of payload; this is a structural property of the schema, not a filter.

## Buyer requirements

Any x402-capable agent. A plain EVM private key is sufficient (e.g.
`x402`'s `EthAccountSigner`) — no CDP account, no Inferrail account, no
prior relationship with Inferrail is required. See
[`examples/work_economics_purchase.py`](../../examples/work_economics_purchase.py)
for a complete, standalone buyer.

## Endpoint

```
GET  <base_url>/manifest    # machine-readable capability description, no payment required
GET  <base_url>/health      # liveness only
POST <base_url>/invoke      # the paid capability (x402-gated)
```

`GET /manifest` returns this same contract as JSON — capability id/version,
price, network, `pay_to` address, and the request/response JSON Schemas —
so an agent that has only discovered the base URL (via the x402 Bazaar
listing, `llms.txt`, or otherwise) never needs private knowledge to use it.

Inferrail's own hosted instance: `https://work.tryinferrail.com` —
[manifest](https://work.tryinferrail.com/manifest),
[human-readable overview](https://tryinferrail.com/work-economics/).

## Request

`POST /invoke`, header `X-Purchase-Id: <buyer-chosen idempotency key>`:

```json
{
  "work_id": "string",
  "events": [
    {
      "resource_class": "string",
      "supplier": "string",
      "known_cost_usd": "decimal string or null",
      "price_basis": "LIST_PRICE_PUBLISHED | PROVIDER_REPORTED_ESTIMATE | UNKNOWN",
      "currency": "USD",
      "status": "success | error | partial"
    }
  ],
  "outcome_status": "string or null"
}
```

`events` must have at least one item. `known_cost_usd` is required (a
decimal string, never a float) unless `price_basis` is `UNKNOWN`, in which
case it must be `null`.

An unpaid call returns **HTTP 402** with x402 `PaymentRequirements`
(scheme, network, asset, amount in atomic units, `pay_to`). Retry the same
request with the signed `X-PAYMENT` header the 402 response asked for, and
the **same `X-Purchase-Id`** — reusing a purchase id never charges or
executes twice; it returns the already-computed result.

## Response

`200 OK`:

```json
{
  "result": {
    "work_id": "string",
    "known_total_cost_usd": "decimal string or null",
    "exact_total_known": true,
    "event_count": 1,
    "unknown_event_count": 0,
    "breakdown_by_resource_class": {"inference": "0.02"},
    "breakdown_by_supplier": {"example-supplier": "0.02"},
    "price_provenance": {"LIST_PRICE_PUBLISHED": 1},
    "outcome_status": "success",
    "capability_version": "work-economics-v1"
  },
  "commercial_receipt": {
    "purchase_id": "string",
    "invocation_id": "uuid",
    "capability": "inferrail-work-economics",
    "capability_version": "work-economics-v1",
    "rail": "X402_BASE_SEPOLIA_TESTNET",
    "network": "eip155:84532",
    "network_class": "TESTNET",
    "quoted_amount_usd": "0.05",
    "currency": "USD",
    "work_id": "string",
    "status": "DELIVERED"
  },
  "newly_charged": true,
  "newly_executed": true
}
```

`422` with `{"result": {"error": "..."}, "commercial_receipt": {..., "status": "INPUT_REJECTED"}}`
if the request body fails validation after payment — the payment is still
verified/settled (you were charged for an invocation, even a rejected one),
which is why the receipt's `status` distinguishes `INPUT_REJECTED` from
`DELIVERED`.

## Price

Fixed **$0.05 USD** per invocation, quoted and settled in test-USDC on Base
Sepolia. One price per call — not metered by event count or work
complexity. `network_class: "TESTNET"` is stamped on every receipt so a
testnet purchase is never mistaken for real revenue.

## Reconciling a purchase independently

`purchase_id` (yours), `invocation_id` (Inferrail's, deterministic from
`purchase_id`), and the on-chain settlement transaction (from the
`PAYMENT-RESPONSE` header, or Base Sepolia's own public block explorer)
together let any party — buyer, Inferrail, or a third-party auditor —
independently confirm that one purchase id corresponds to exactly one
payment and exactly one delivered result, without needing to trust either
side's bookkeeping.
