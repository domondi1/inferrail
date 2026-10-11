# Inferrail Hosted

**Status: experimental. Credit purchases are not open yet. Pricing is an experiment, not
validated demand.**

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

Two keys, never mixed up:

| Key | What it is | Where it goes |
|---|---|---|
| **Workspace key** (`irw_...`) | Identifies your workspace on Inferrail Hosted. Shown once at creation; only its hash is stored | `Authorization: Bearer <key>` or `x-api-key: <key>`: what an SDK sends for `api_key=` |
| **Provider key** (`sk-...`) | Your own OpenAI or Anthropic key. Your provider bills you | `X-Provider-Api-Key` on every request. Read per request, never stored or logged |

```bash
curl -s -X POST https://<host>/v1/workspaces    # returns {"api_key": "irw_...", ...} once
```

OpenAI SDK (`pip install openai`):

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<host>/v1",
    api_key="irw_...",  # workspace key
    default_headers={"X-Provider-Api-Key": "sk-..."},  # your OpenAI key: never stored
)
client.chat.completions.create(
    model="gpt-4o-mini",
    max_tokens=200,
    messages=[{"role": "user", "content": "Summarize this contract."}],
    extra_headers={
        "X-Inferrail-Attribute-Work-Id": "contract-review-42",  # the run
        "X-Inferrail-Budget-Usd": "0.50",  # its ceiling
    },
)
```

Anthropic SDK (`pip install anthropic`). Note the base URL has **no** `/v1`: the SDK adds it.

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="https://<host>",
    api_key="irw_...",  # workspace key
    default_headers={"X-Provider-Api-Key": "sk-ant-..."},  # your Anthropic key: never stored
)
client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=200,
    messages=[{"role": "user", "content": "Summarize this contract."}],
    extra_headers={
        "X-Inferrail-Attribute-Work-Id": "contract-review-42",
        "X-Inferrail-Budget-Usd": "0.50",
    },
)
```

**Budgets:**
- The first ceiling declared for a work id is kept for the whole run. A different value later
  gets a 400, never a silent change.
- Without `X-Inferrail-Budget-Usd`, a call is metered and receipted but **not capped**.
- A call that would cross the ceiling gets a 402 (`BudgetExceededError`, `INFERRAIL_E010`)
  before anything reaches the provider.

**Usage and receipts:**
- `GET /v1/work/contract-review-42` lists the run's calls, cost and receipts.
- `GET /v1/workspace` shows usage and credits.
- `GET /v1/workspace/ledger` lists every credit grant, refund and pending purchase.

**For agents:**
- `GET /llms.txt` is the whole contract in plain text: headers, errors and retries, pricing,
  and how to buy credits when a rail is open. It's generated from the running configuration.
- `GET /pricing` is the same contract as JSON; `GET /openapi.json` is the schema.

**Errors you can act on:**

| Status | Meaning | Do |
|---|---|---|
| 400 | Missing provider key, bad budget header, or a conflicting ceiling | Fix the request |
| 401 | Missing or invalid workspace key | Check the key, or create a workspace |
| 402 `BudgetExceededError` | The run's ceiling would be exceeded | Not retryable as-is |
| 402 `allowance_exhausted` | Free runs used, no credits | Buy credits, or wait for `free_allowance_resets_at` |
| 429 | Workspace rate limit | Wait `Retry-After` seconds |
| 502 / 504 | Provider error or timeout | `error.remediation` says whether to retry |

To try it locally with no payments: `python hosted/budget_gateway/service.py`, then use
`http://localhost:8424`. A runnable multi-agent example is in
[`examples/hosted_shared_budget/`](../../examples/hosted_shared_budget/).

## Billing (experimental pricing)

| Term | Definition |
|---|---|
| **Governed run** | A distinct work id (`X-Inferrail-Attribute-Work-Id`) in a UTC month. A call without a work id is its own run |
| **When a run is billed** | Only when at least one of its calls is **answered by the provider**. Runs whose calls all fail (provider error, timeout) or are refused by their own budget **cost nothing**. Later calls in a billed run cost nothing more |
| **Free** | 250 governed runs per workspace per month |
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

## Running it inside the existing trial gateway (one service, no new instance)

`cohost.py` serves both apps from one process. The trial API (`/try/` page backend) stays at
`/`, unchanged, and Inferrail Hosted is mounted at `/hosted`.

- **Code:** each app keeps its own module names. Trial tokens and Hosted workspace keys are
  unknown to each other.
- **Data:** trial data stays in `COST_GATEWAY_DATA_DIR` (ephemeral, as before). Hosted's
  money-critical state goes in `BG_DATA_DIR`, which must be the attached disk.
- **Startup:** both apps' lifecycles run (the trial purge loop and Hosted reconciliation).
- **Memory:** measured on Python 3.12 with the x402 rail enabled, about 100 MB resident after
  about 3,200 mixed requests (trial alone: about 69 MB). The 512 MB instance has ample headroom.
- **Commands:**

  | Setting | Value |
  |---|---|
  | Build | `pip install -e . -r hosted/cost_gateway/requirements.txt -r hosted/budget_gateway/requirements.txt` |
  | Start | `python3 hosted/budget_gateway/cohost.py` |
  | Env | `BG_DATA_DIR=/var/data/hosted` on the disk, `BG_PUBLIC_BASE_URL=https://<host>/hosted` |

- **Rollback:** set the start command back to `python3 hosted/cost_gateway/service.py`. The
  trial is unaffected, and Hosted's data stays on the disk.

## Deployment constraints

- **Exactly one instance per data directory**, with one uvicorn worker.
  - Many client machines may call it; that's the "shared across your fleet" guarantee.
  - **Multiple gateway replicas sharing state are not supported and not claimed.**
  - `create_app` takes an exclusive `flock` on `BG_DATA_DIR/instance.lock` and refuses to start
    if another instance holds it.
- `BG_DATA_DIR` must be a **local persistent disk**. `flock` and SQLite WAL are unreliable on
  network filesystems.
- Run it behind TLS. The per-IP workspace-creation throttle reads the client address from
  `BG_CLIENT_IP_HEADER` (default `true-client-ip`, which Render's Cloudflare edge sets and
  clients can't spoof), falling back to the peer address.
- **Mainnet x402 requires:** `BG_X402_MAINNET_APPROVED=1`, `BG_BASE_RPC_URL` (for
  reconciliation; startup refuses without it), and `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` for
  the facilitator.

| Variable | Default | Meaning |
|---|---|---|
| `BG_DATA_DIR` | `/tmp/inferrail_budget_gateway` | Persistent storage |
| `BG_FREE_RUNS_PER_MONTH` | `250` | Free governed runs per workspace per month |
| `BG_PER_WORK_MAX_USD` | `100` | Largest per-run ceiling a caller may declare |
| `BG_WORKSPACE_CREATION_ENABLED`, `BG_MAX_WORKSPACES`, `BG_CREATIONS_PER_IP_PER_HOUR` | `1`, `10000`, `5` | Abuse guards |
| `BG_MAX_BODY_BYTES` | `2000000` | Request size limit (by `Content-Length`) |
| `BG_X402_PAY_TO`, `BG_X402_NETWORK` | unset (rail off), `eip155:84532` | Agent credit purchases |
| `BG_BASE_RPC_URL`, `BG_RECONCILE_SECONDS` | unset, `60` | Chain reconciliation of pending x402 purchases |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | unset (rail off) | Card purchases; webhook events `checkout.session.completed`, `charge.refunded` |
| `BG_PUBLIC_BASE_URL` | `http://localhost:8424` | Public URL used in links |
| `BG_CLIENT_IP_HEADER` | `true-client-ip` | Trusted header carrying the real client address |
| `BG_MAX_DAILY_SALES_USD` | `100` | Bounded experiment: new credit purchases are refused (503) once confirmed plus pending sales in the last 24 hours would exceed this |
| `BG_MAX_OUTSTANDING_CREDITS_USD` | `100` | Refuses new credit purchases (503) while the value of all unspent prepaid credits plus pending purchases would exceed this. Separate from the daily cap |
| `BG_RATE_LIMIT_PER_MINUTE` | `600` | Per-workspace authenticated request limit (429) |
| `BG_MOUNT_PREFIX` | empty (`/hosted` when cohosted) | Path prefix when mounted inside another app |

## Known limits (before real money)

- **x402 refunds** are manual: the operator sends USDC back from the receiving wallet, then
  records the reversal with `ops.py refund-x402 <payer:nonce> <cents>`.
- **Stripe disputes** (chargebacks) are not handled automatically. Treat a dispute as a refund.
- **No migrations:** the ledger schema is v1. A schema change needs a migration before deploy.
- **Backups:** `python3 hosted/budget_gateway/ops.py backup /var/data/backups` takes a
  consistent online copy. Restoring an old copy can re-open spent credits, so reconcile
  against Stripe and the chain after any restore.
- **Operations:**
  - `ops.py liabilities` reports outstanding prepaid value, pending purchases and net
    revenue;
  - `ops.py reconcile` resolves pending x402 purchases from the chain.

Tests: `python -m pytest tests/unit/hosted/test_budget_gateway.py` (needs the `hosted` extra).
