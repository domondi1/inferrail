# Inferrail Hosted

**Status: experimental. Not deployed. Pricing is an experiment, not validated demand.**

The Inferrail gateway, run for you. Give each AI run a dollar ceiling in a request header. Every
call in that run, from any agent or machine using the same workspace, draws on one ledger. A
call that would push the run past its ceiling is refused before it reaches OpenAI or Anthropic.
Receipts stay payload-free. You bring your own provider key, sent with each request and never
stored.

## What it adds to the free, self-hosted gateway

The open-source gateway (`pip install inferrail`) already enforces per-run budgets on one
machine, and **this service doesn't change or restrict it.**

Inferrail Hosted is for when you'd rather not run it yourself, or when many agents and machines
must share one budget. It adds:
- operation: a durable workspace;
- one central ledger for every client of the workspace;
- self-service billing.

## Quickstart

```bash
curl -s -X POST https://<host>/v1/workspaces    # returns {"api_key": "irw_...", ...} once
```

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<host>/v1",
    api_key="irw_...",                                   # workspace key
    default_headers={"X-Provider-Api-Key": "sk-..."},   # your OpenAI key: never stored
)
client.chat.completions.create(
    model="gpt-4o-mini",
    max_tokens=200,
    messages=[{"role": "user", "content": "Summarize this contract."}],
    extra_headers={
        "X-Inferrail-Attribute-Work-Id": "contract-review-42",   # the run
        "X-Inferrail-Budget-Usd": "0.50",                        # its ceiling
    },
)
```

- `GET /v1/work/contract-review-42` lists the run's calls, cost and receipts.
- `GET /v1/workspace` shows usage and credits.
- `GET /v1/workspace/ledger` lists every credit grant, refund and pending purchase.
- Anthropic works the same way through `POST /v1/messages`.

To try it locally with no payments: `python hosted/budget_gateway/service.py`, then use
`http://localhost:8424`.

## Billing (experimental pricing)

| Term | Definition |
|---|---|
| **Governed run** | A distinct work id (`X-Inferrail-Attribute-Work-Id`) in a UTC month. A call without a work id is its own run |
| **When a run is billed** | Only when at least one of its calls is **answered by the provider**. Runs whose calls all fail (provider error, timeout) or are refused by their own budget **cost nothing**. Later calls in a billed run cost nothing more |
| **Free** | 2,000 governed runs per workspace per month |
| **After that** | Prepaid credits, $0.001 per run. Agents: `POST /v1/credits/x402`, $1 in USDC for 1,000 runs, no account. People: `POST /v1/credits/checkout` (Stripe), $10 for 10,000 runs |
| **No surprise charges** | No subscription, no auto top-up, no overage billing. Past the allowance with no credits, a call gets `402 allowance_exhausted`, listing how to buy, and nothing reaches the provider |
| **Model spend** | Billed by your provider on your own key. Inferrail never resells model access |
| **Refunds** | A Stripe refund reverses the matching credits automatically (proportional for partial refunds). A negative balance blocks paid runs until topped up |

While a run's first call is in flight, the run *holds* one unit (a free slot, or one credit), so
concurrent new runs can't overspend. The hold is released if no call is answered.

## Guarantees and how they're tested

| Guarantee | Mechanism | Test |
|---|---|---|
| Concurrent calls in one run can't overspend its budget | The self-hosted gateway's atomic reservation (`BudgetEnforcer`), per workspace | `test_concurrent_calls_in_one_run_cannot_overspend_its_budget` (12 parallel calls) |
| A run's spend survives restarts | Reservations and receipts are SQLite, `synchronous=FULL` on the billing ledger | `test_budget_state_survives_a_restart` |
| Workspaces are isolated | Separate receipt/budget files per workspace; keys resolve to one workspace | `test_workspaces_are_isolated` |
| One credit can't be spent twice | Admission and credit consumption in one `BEGIN IMMEDIATE` transaction | `test_concurrent_new_runs_never_spend_one_credit_twice` (25 threads) |
| A crash mid-call doesn't leak a held credit | Held runs are released at startup; billed runs are kept | `test_restart_releases_orphaned_holds_but_keeps_billed_runs` |
| A payment grants credits at most once | `UNIQUE(source, ref)`: Stripe session id; x402 payer:nonce | `test_a_payment_reference_grants_at_most_once`, webhook replay tests |
| A checkout redirect grants nothing | Only a signature-verified `checkout.session.completed` with `payment_status=paid` and the published amount grants | `test_checkout_redirect_alone_grants_nothing`, `…only_for_paid_matching_sessions` |
| An interrupted webhook is credited exactly once | The grant is one transaction; Stripe retries non-2xx | `test_interrupted_webhook_processing_grants_exactly_once_on_retry` |
| A settled x402 payment is never lost | Recorded *pending* before settlement; credited by the settle hook, **or** by on-chain reconciliation (USDC `authorizationState`) if the process dies in between | `test_a_settled_x402_payment_is_not_lost_when_the_grant_crashes`, `test_reconciliation_resolves_pending_purchases_from_chain_truth` |
| Failed x402 settlement grants nothing | Pending stays until the chain decides; expired and unused → failed | `test_x402_settlement_failure_grants_nothing` |
| An invalid workspace is never charged | The x402 handler returns 401, so the middleware never settles | `test_x402_payment_with_an_invalid_workspace_is_never_settled` |
| No spending a nonexistent balance | Paid runs need a balance > 0; refunds can make it negative, which blocks | `test_refunds_reverse_credits_monotonically_and_block_negative_balances` |
| Provider keys never at rest or echoed | Read per request; scrubbed from error text; unhandled errors log the type only | `test_provider_key_is_forwarded_but_never_stored_or_returned`, `test_provider_errors_never_echo_the_key_and_are_not_billed` |
| Workspace keys never at rest | SHA-256 hashes only | `test_workspace_keys_are_stored_only_as_hashes` |

## Deployment constraints

- **Exactly one instance per data directory**, with one uvicorn worker.
  - Many client machines may call it; that's the "shared across your fleet" guarantee.
  - **Multiple gateway replicas sharing state are not supported and not claimed.**
  - `create_app` takes an exclusive `flock` on `BG_DATA_DIR/instance.lock` and refuses to start
    if another instance holds it.
- `BG_DATA_DIR` must be a **local persistent disk**. `flock` and SQLite WAL are unreliable on
  network filesystems.
- Run it behind TLS. Set `BG_FORWARDED_ALLOW_IPS` to your proxy so the per-IP
  workspace-creation throttle sees real client addresses.
- **Mainnet x402 requires:** `BG_X402_MAINNET_APPROVED=1`, `BG_BASE_RPC_URL` (for
  reconciliation; startup refuses without it), and `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` for
  the facilitator.

| Variable | Default | Meaning |
|---|---|---|
| `BG_DATA_DIR` | `/tmp/inferrail_budget_gateway` | Persistent storage |
| `BG_FREE_RUNS_PER_MONTH` | `2000` | Free governed runs per workspace per month |
| `BG_PER_WORK_MAX_USD` | `100` | Largest per-run ceiling a caller may declare |
| `BG_WORKSPACE_CREATION_ENABLED`, `BG_MAX_WORKSPACES`, `BG_CREATIONS_PER_IP_PER_HOUR` | `1`, `10000`, `5` | Abuse guards |
| `BG_MAX_BODY_BYTES` | `2000000` | Request size limit (by `Content-Length`) |
| `BG_X402_PAY_TO`, `BG_X402_NETWORK` | unset (rail off), `eip155:84532` | Agent credit purchases |
| `BG_BASE_RPC_URL`, `BG_RECONCILE_SECONDS` | unset, `60` | Chain reconciliation of pending x402 purchases |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | unset (rail off) | Card purchases; webhook events `checkout.session.completed`, `charge.refunded` |
| `BG_PUBLIC_BASE_URL` | `http://localhost:8424` | Public URL used in links |
| `BG_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Trusted proxy addresses for client IPs |

## Known limits (before real money)

- **Request limits:** no per-workspace request rate limit (provider spend is on the customer's
  own key; CPU abuse is bounded only by the proxy). Chunked request bodies without
  `Content-Length` aren't size-checked.
- **x402 refunds** are manual: the operator sends USDC back, then records the reversal through
  `WorkspaceLedger.apply_refund("x402", ref, cents)`. There's no HTTP route for it.
- **Stripe disputes** (chargebacks) are not handled automatically. Treat a dispute as a refund.
- **No migrations:** the ledger schema is v1. A schema change needs a migration before deploy.
- **Backups:** copy `BG_DATA_DIR` with SQLite's online backup. Restoring an old copy can
  re-open spent credits, so reconcile against Stripe and the chain after any restore.

Tests: `python -m pytest tests/unit/hosted/test_budget_gateway.py` (needs the `hosted` extra).
