# Job-Safe Web Search

Standalone hosted x402 search capability. One bounded POST /search call
returns ranked title, URL, and snippet results with a machine-readable receipt.
Configured default price: $0.010 USDC on Base; this experimental capability is not yet live on mainnet.

The search query is sent to the configured supplier (Serpex or Exa) for processing. The service keeps purchase
records and successful output to protect retries and provide five-minute cache
reuse. Mainnet stays disabled until supplier terms explicitly permit this
integration, output delivery, and storage.

Payment uses x402 exact. Settlement occurs before calling the supplier. This is
an upfront purchase: if payment settles and the supplier later fails, the
service returns HTTP 202 with an unresolved receipt. It does not claim that the
buyer was not charged and does not promise an automatic refund. Retry with the
same request id and original payment signature, or use the returned job token
for reconciliation. Never sign a second payment for the same request. A
rejected authorization consumes its request ID; retry it with a fresh request
ID and signature.

Before supplier dispatch the service checks the USDC transfer and EIP-3009
nonce event on Base. It does not incur supplier expense when verification,
settlement, or finality is uncertain. Supplier dispatch is recorded durably
before the call. If timeout or restart leaves supplier cost unknown, the
service freezes the purchase for reconciliation and never retries or fails
over.

## Local testnet

Install hosted/job_safe_search/requirements.txt, then set SEARCH_PAY_TO,
SEARCH_RESOURCE_URL, SEARCH_TOKEN_SECRET (at least 32 random bytes),
SEARCH_RPC_URL, SEARCH_DB_PATH, SEARCH_NETWORK=eip155:84532,
CDP_API_KEY_ID, CDP_API_KEY_SECRET, and SEARCH_ALLOW_NEW_DB=1 for the
intentional first start. Run python -m hosted.job_safe_search.service.
Testnet uses a deterministic fixture supplier and incurs no paid supplier
expense. An independent test wallet is required for real Base Sepolia
settlement. Never send mainnet funds to this configuration.

## Mainnet startup gates

The production factory refuses Base mainnet unless all gates are satisfied:

- SEARCH_MAINNET_APPROVED=1 records explicit approval to accept real money.
- SEARCH_SUPPLIER_RIGHTS_CONFIRMED=1 records written search-output resale,
  integration, redistribution, and caching permission.
- SEARCH_PAYMENT_FEE_BOUND_USD covers the actual account schedule and all
  incremental payment costs. A static realized mainnet fee is rejected.
- SEARCH_RESOURCE_URL is public HTTPS and SEARCH_PAY_TO is the approved
  merchant wallet.
- SEARCH_SUPPLIER explicitly selects serpex or exa, with its credential.
- A private SEARCH_EXCLUDED_WALLETS_PATH exists and validates.
- The fixture supplier is testnet-only.

The configured price must cover the supplier bound, payment-fee bound, and
minimum contribution floor. The default minimum floor is $0.005. An Exa
configuration needs a higher price than the $0.010 Serpex default.
Unresolved exposure plus prepaid supplier capital must not exceed $20.
The mainnet template reserves $5 prepaid capital and $15 unresolved exposure. Each uncertain purchase reserves its
full price plus maximum supplier cost and payment fee. Unknown costs keep
margin unrecognized.

SEARCH_DB_PATH must use durable storage. SEARCH_ALLOW_NEW_DB=1 is only for the
intentional first start and must then be unset. When unresolved purchases
exist, SEARCH_RECOVERY_FROM_BLOCK must identify the first block from which
merchant payments may exist. Startup reconciles uncertain payment and supplier
states without resending payment or repeating the supplier call.

The database binds to its network, token contract/domain, merchant address and
capability-key digest. A changed payment domain or token secret refuses startup;
existing ledgers are checked before their first binding. Preserve the original
secret during upgrade. Changing price does not change this domain binding.
A POSIX process lock holds the entire service lifespan and rejects a second
worker using the same database. Preserve the adjacent .lock file while a
service is active. Run a single worker against SQLite. Reconcile all chain transactions before
restoring any backup that may predate an accepted payment.

## Request

Required fields: query, request_id. Optional fields: num_results (1 to 5),
job_id, job_token, job_budget_usd. Money is a decimal string with at most six
places. A job budget cannot be raised after the first authenticated request.
The opaque job token binds a job to its payer wallet. The same authenticated
job and canonical query can reuse results for five minutes without another
charge or supplier call.

## External metrics

Keep founder, company, deployment, test, and bootstrap wallets in a private
exclusion file. Run:

python -m hosted.job_safe_search.metrics /persistent/search.sqlite3 --exclude-wallets /secure/private/excluded-wallets.txt --hosting-cost-usd 0.00

The CLI report counts only Base mainnet payments from non-excluded wallets and separates settled revenue from
realized contribution margin. Hosting cost is separate and must be supplied
from an actual hosting bill; omit it and strict experiment P&L stays unknown.
Unresolved supplier costs or payment fees remain unresolved.


## Real Sepolia HTTP/process exercise

`python -m hosted.job_safe_search.testnet_e2e --wallet-file /secure/testnet-wallets.json --state-dir /secure/new-run --run-actual-testnet`

This opt-in runner starts two local HTTP servers and uses CDP to settle actual
Sepolia USDC for inbound and outbound payments. It exercises cache reuse,
immutable/exhausted budgets, fresh-payment replay, and process death after
settlement and after supplier payment. It refuses an existing state directory
and never targets mainnet. Use three dedicated testnet EOA wallets; exclude
all of them permanently. Confirmation in this fast test runner checks canonical
mined receipts; the production factory requires finalized blocks.

## Container deployment and recovery

Copy `mainnet.env.example` to the ignored `.env`, populate operator-approved
values, and place the private exclusions in `private/excluded-wallets.txt`.
Create `data/` owned by UID 10001. From the repository root, build with:

`docker compose -f hosted/job_safe_search/compose.yaml build`

Starting the prepared container requires approval for live payment acceptance:

`docker compose -f hosted/job_safe_search/compose.yaml up -d`

Use a TLS reverse proxy to port 8422. Run one worker and one database writer.
The merchant and treasury private keys are never required by this server.
For prepaid API suppliers, no on-chain operational purchaser key is required;
limit the supplier credential to the approved prepaid balance and disable
automatic top-ups. The recovery block must be captured before first acceptance.
Remove SEARCH_ALLOW_NEW_DB after the deliberate first initialization.

After a crash, preserve the database and restart the same image/configuration.
Startup first freezes interrupted supplier intents locally. Read-only background
reconciliation advances finalized payments without resending authorizations.
An unavailable receipt does not prevent other purchases from recovering or
completed results from replaying. Event scans use bounded block ranges and
continue across passes; only finalized ranges may be skipped on later passes.
Restarting rescans history safely. Repeated outages retain unknown financial
states and create deduplicated recovery-deferred events.
An interrupted supplier purchase stays frozen until its billing record is
reconciled. Never retry an ambiguous supplier purchase. Record invoice/request
references and confirmed refund evidence using Store.resolve_financials;
outstanding credits remain liabilities. Keep a SQLite online backup and
append-only event export before each release.

To roll back, stop accepting requests, preserve the current database, and pin
the previous compatible image. Never restore a stale database while payments
may have settled: reconcile every authorization since the recorded recovery
block first. Database loss requires closing payment acceptance until recovery;
a fresh database must not be used to bypass replay protection.

Validate the deployed endpoint without payment using CDP POST
`https://api.cdp.coinbase.com/platform/v2/x402/validate` with JSON `{ "resource": "https://YOUR_HOST/search", "method": "POST" }`. Confirm health, 402 amount/network/payTo, declared
schemas and discovery examples before activation. Validation does not index
an endpoint. Check the live discovery catalogue and merchant resources after
settlement. If one controlled indexing payment is necessary, obtain explicit
approval for exactly one transaction, label INDEXING_BOOTSTRAP, persist its
signature before dispatch, and exclude the payer forever. Never repeat it to
increase usage counters.

If a proxy independently settles a second payment before forwarding a completed
request, the service records that extra transfer as an append-only liability,
reports it in the receipt/metrics, and does not buy the supplier again. It
never silently recognizes that money as margin. Such externally settled
duplicates require explicit reconciliation; the server never issues refunds
or signs a second payment automatically.

After an approved full duplicate-payment refund actually settles, an operator
may record its confirmed transaction, exact refund fees and durable evidence
with `Store.reconcile_extra_refund`. This performs no payment. Pending incoming
transfers cannot be reconciled as refunded until finalized, and duplicate
refund records are rejected. Reconciled refund costs are deducted from margin.

Supplier billing is recorded before output validation. A billed malformed or empty response retains its known cost and full unresolved customer liability; it is never retried. An observed supplier cost above the reserved bound blocks new payments and dispatches for that provider across restarts. Resolve the supplier contract before changing provider configuration; reconciliation records must preserve actual incurred costs. Empty supplier credentials are rejected at startup.

Metrics read purchases and events from one read-only SQLite snapshot. The operator report includes per-wallet settlement references, distinct-query counts, resolved positive-margin calls, and first/last delivery times. Excluded wallets are omitted from this evidence. A repeat payer with unknown supplier costs is not counted as repeat positive-margin usage.

Discovery examples use the configured price, supplier, job budget, and cache lifetime. The atomic reservation path uses the same cache lifetime as the initial cache lookup, so an expired result cannot become a free cache hit during reservation.

Signed payments accompanying rejected input or a replay are observed durably before RPC reads. They are never submitted for settlement. Independently finalized transfers remain customer liabilities even when no purchase could be created. RPC failure or an absent transaction keeps the observation unresolved; only finalized authorization expiry and an unspent token nonce prove nonpayment. Metrics include these observations and suppress final contribution while any remain unresolved.
An observed payment cannot be rebound to a different purchase. A settled payment for rejected input requires explicit financial reconciliation; correcting the input never authorizes another charge or automatically spends supplier credits.
An operator may call `Store.reconcile_unfulfilled_refund(payer, nonce, refund_transaction=..., variable_fees=..., evidence=...)` only after independently confirming a full refund. `variable_fees` is the actual total payment/refund cost in atomic USDC units. This records evidence and sends no transaction. Any refund fee remains a realized loss; a refund never becomes profit.
Refund reconciliation must include actual incoming-payment and refund costs. Unresolved additional payments leave total variable fees unknown. The risk ceiling uses the larger of reserved bounds and recorded actual costs, so switching suppliers cannot conceal an earlier overrun.

For a real payment-ordering proof, run the opt-in Sepolia harness with `--require-finalized` and `--scenarios success`. It waits for finalized incoming USDC before merchant supplier dispatch and finalized outgoing USDC before the fixture delivers. HTTP timeouts retry only the original request/signature. This mode may take two finality windows; the default fast fixture mode reports canonical mined receipts explicitly and is not a finalized ordering proof.

Mainnet payment fees remain unknown until actual per-purchase billing evidence
is recorded with `Store.resolve_financials`. A fee ceiling protects dispatch;
it is not realized COGS. Account free tiers must not be assumed to apply to
every purchase. Successful output can be delivered with unknown fees, but its
receipt and metrics retain unknown margin and the unresolved exposure stays
reserved. Reconcile actual fees, including taxes and any other variable costs,
before counting that purchase toward realized contribution.

Known payment fees above the current ceiling block new payment
acceptance and supplier dispatch across restarts. A fee-ceiling increase also
freezes purchases reserved under a lower ceiling. Reconcile those purchases
and the current cost envelope before resuming; completed results still replay.

Finalized nonpayment proof establishes zero customer revenue and zero supplier
spend. It does not establish that an attempted onchain settlement had zero fees.
Those fees remain unknown until billing is reconciled with
`Store.resolve_financials` on the rejected purchase. Actual failed-payment fees
are recorded as negative contribution, never as paid calls.

If the original supplier provides a read-only retrieval of an already-paid
result, independently verify its original request identity, billing and output.
An operator may record that output with `Store.reconcile_result`, supplying the
original fingerprint/provider/reference and durable evidence. This makes no
network call or payment, refuses identity/cost conflicts and refunds/credits,
and preserves unknown payment fees. A supplier that cannot retrieve an existing
result must stay unresolved; running a new search is not recovery. Supplier
failure events retain only exception classes, never exception text or credentials.
