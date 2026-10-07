# Inferrail Job-Safe Web Search

**Status:** implementation prepared; not deployed or accepting payments.
The first release target is Base Sepolia testnet. Base mainnet requires a
separate explicit approval, written supplier rights, durable hosting, and
confirmed per-call payment costs.

One x402-protected POST /search call returns up to five ranked web results
with title, URL, snippet, and a machine-readable economic receipt. The
currently prepared price is $0.015 USDC on Base.

## Payment behavior

The service uses x402 v2 exact on Base. An unsigned request returns HTTP 402
with payment requirements and Bazaar discovery metadata. It settles the exact
USDC transfer and checks the on-chain transaction and EIP-3009 nonce event
before calling the search supplier.

This is an upfront purchase. A settled payment followed by a supplier failure
can leave the buyer charged without results. Such requests return HTTP 202 and
an unresolved receipt. The service does not promise an automatic refund.
Retries must use the same request ID and original payment signature, or the
returned job token. A fresh payment for a completed request ID is never
settled again.

## Request contract

Query and buyer-controlled request_id are required. num_results accepts 1–5.
Optional job_id is an identifier, not authorization. A returned opaque
job_token binds the job and its immutable optional job_budget_usd to the
paying wallet.

The same authenticated job and canonical query may reuse a successful result
for five minutes at no extra charge or supplier cost. A repeated request ID
with a different query is rejected. An exhausted job budget is refused before
payment.

## Economics and failure handling

The configured envelope is $0.015 customer revenue, up to $0.007 search
supplier cost, up to $0.001 payment cost, and a minimum $0.005 contribution
margin. The global unresolved exposure limit is $20. No supplier call begins
unless customer settlement and chain finality have been checked.

The service uses one supplier and does not retry or fail over after dispatch.
If settlement or supplier cost is uncertain, it preserves the unresolved
state and refuses to recognize contribution margin. Its metrics exclude
operator-controlled wallets from external usage.

See the hosted service runbook at
../../hosted/job_safe_search/README.md for the testnet procedure, production
gates, persistent storage, reconciliation, and external metrics.
