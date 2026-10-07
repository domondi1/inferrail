---
name: inferrail-job-safe-search
description: Search the web through Inferrail with upfront x402 USDC payment, durable request IDs, job budgets, safe retry instructions, and a machine-readable receipt.
---

# Inferrail Job-Safe Web Search

This capability is not yet deployed. Do not send payment until the resource
advertises Base mainnet exact USDC and a current price.

When a live resource is available:

1. Read its x402 payment challenge and Bazaar schema.
2. Send POST /search with a useful query and a unique request_id.
3. Check the advertised price and network before signing exact payment.
4. On HTTP 202, reuse the same request ID and original payment signature, or
   use the returned job_token. Never sign a second payment for that request.
5. Save the returned job token if continuing the same job. A job budget cannot
   be raised after the first authenticated request.
6. Treat financial_state UNRESOLVED as unresolved; do not infer that a supplier
   ran once or that a refund occurred.

Optional request fields are num_results (1–5), job_id, job_token, and
job_budget_usd. job_id alone is not authorization.
