# Job-Safe Web Search

Standalone hosted x402 search capability. One bounded POST /search call
returns ranked title, URL, and snippet results with a machine-readable receipt.
Price: $0.015 USDC on Base.

The search query is sent to Exa for processing. The service keeps purchase
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
- SEARCH_REALIZED_PAYMENT_FEE_USD contains a known per-call payment fee.
- SEARCH_RESOURCE_URL is public HTTPS and SEARCH_PAY_TO is the approved
  merchant wallet.
- An Exa API key is present; the fixture supplier is testnet-only.

The price/cost envelope is $0.015 revenue, at most $0.007 supplier cost, at
most $0.001 payment costs, and at least $0.005 minimum contribution margin.
The unresolved exposure ceiling is $20. Each uncertain purchase reserves its
full price plus maximum supplier cost and payment fee. Unknown costs keep
margin unrecognized.

SEARCH_DB_PATH must use durable storage. SEARCH_ALLOW_NEW_DB=1 is only for the
intentional first start and must then be unset. When unresolved purchases
exist, SEARCH_RECOVERY_FROM_BLOCK must identify the first block from which
merchant payments may exist. Startup reconciles uncertain payment and supplier
states without resending payment or repeating the supplier call.

Run a single worker against SQLite. Reconcile all chain transactions before
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

The report counts non-excluded wallets and separates settled revenue from
realized contribution margin. Hosting cost is separate and must be supplied
from an actual hosting bill; omit it and strict experiment P&L stays unknown.
Unresolved supplier costs or payment fees remain unresolved.
