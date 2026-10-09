# Inferrail Hosted

The Inferrail gateway, run for you. Per-run dollar budgets hold across **every agent and machine**
that calls the same workspace, and calls that would overspend a run are refused before they reach
the provider. Receipts stay payload-free. You bring your own OpenAI or Anthropic key, sent with
each request and never stored.

The open-source gateway (`pip install inferrail`) is unchanged and unrestricted. Run it yourself
for free. This service is for teams that want one shared budget ledger for a fleet without
operating it.

## Use it

```bash
curl -X POST https://<host>/v1/workspaces        # returns api_key once
```

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<host>/v1",
    api_key="irw_...",                                      # your workspace key
    default_headers={"X-Provider-Api-Key": "sk-..."},      # your OpenAI key, never stored
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

- `POST /v1/messages` does the same for Anthropic.
- `GET /v1/work/{work_id}` shows what a run cost.
- `GET /v1/workspace` shows usage and remaining credits.

## Price

`GET /pricing` and `GET /llms.txt` publish the terms:

- A **governed run** is a distinct work id with at least one call through the gateway in a UTC
  month. Calls within a run are not charged separately.
- 2,000 governed runs per month are free. After that, prepaid credits cost $0.001 per run.
  - **Agents:** `POST /v1/credits/x402` buys 1,000 runs for $1 in USDC (x402 `exact`), with no
    account needed.
  - **People:** `POST /v1/credits/checkout` returns a Stripe Checkout link. $10 buys 10,000 runs.
- No subscription, no sales call. Your provider bills your model usage on your own key;
  Inferrail never resells model access.
- Past the allowance with no credits, a call gets `402 allowance_exhausted`, listing both ways to
  buy. Nothing reaches the provider.

## Money and key safety

| Guarantee | How |
|---|---|
| A payment grants credits at most once | `UNIQUE(source, ref)`: Stripe session id; x402 payer:nonce |
| x402 credits only after settlement | The handler records *pending*; the after-settle hook grants; a failed settlement grants nothing; `pending()` exposes any gap for reconciliation |
| Card credits only from verified events | Stripe signature (HMAC-SHA256, 5-minute tolerance); `payment_status=paid`; the amount must equal the published price |
| No double spend of one credit | Admission and consumption in one `BEGIN IMMEDIATE` transaction (concurrency test) |
| An invalid workspace is never charged | The x402 handler returns 401 before settlement |
| Provider keys never at rest | Read from the request header per call; never written, logged or returned (test reads every data file) |
| Workspace keys never at rest | Stored as SHA-256 hashes only |

## Configure

| Variable | Default | Meaning |
|---|---|---|
| `BG_DATA_DIR` | `/tmp/inferrail_budget_gateway` | Persistent storage (mount a disk) |
| `BG_FREE_RUNS_PER_MONTH` | `2000` | Free governed runs per workspace per month |
| `BG_PER_WORK_MAX_USD` | `100` | Largest per-run ceiling a caller may declare |
| `BG_WORKSPACE_CREATION_ENABLED`, `BG_MAX_WORKSPACES`, `BG_CREATIONS_PER_IP_PER_HOUR` | `1`, `10000`, `5` | Abuse guards |
| `BG_X402_PAY_TO` | unset (rail off) | Receiving address for agent credit purchases |
| `BG_X402_NETWORK` | `eip155:84532` | Base Sepolia. Mainnet `eip155:8453` also needs `BG_X402_MAINNET_APPROVED=1` and CDP API keys |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | unset (rail off) | Card credit purchases |
| `BG_PUBLIC_BASE_URL` | `http://localhost:8424` | Public URL used in links |

Run: `python hosted/budget_gateway/service.py`. Tests:
`python -m pytest tests/unit/hosted/test_budget_gateway.py` (needs the `hosted` extra).
