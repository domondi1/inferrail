# Inferrail Work Economics (hosted)

Inferrail's first hosted, paid capability. See
[`docs/capabilities/work-economics.md`](../../docs/capabilities/work-economics.md)
for the public contract (what it does, request/response schema, price) and
[`docs/adr/0010-hosted-work-economics-capability.md`](../../docs/adr/0010-hosted-work-economics-capability.md)
for why this lives outside `src/inferrail`.

**Base Sepolia testnet only right now.** Not mainnet, not real money.

## Running it locally

```bash
cd hosted/work_economics
pip install -r requirements.txt
export CDP_API_KEY_ID=...           # CDP API credentials (never logged)
export CDP_API_KEY_SECRET=...
export X402_SELLER_PAY_TO_ADDRESS=0x...   # address to receive payment
python3 service.py /tmp/work_economics_test.sqlite3 8421
```

Then, from a separate buyer (see
[`examples/work_economics_purchase.py`](../../examples/work_economics_purchase.py)):

```bash
pip install "x402[evm]" httpx
TESTER_PRIVATE_KEY=0x... python3 examples/work_economics_purchase.py http://127.0.0.1:8421/invoke
```

## Deploying it

Any host that can run a long-lived Python HTTPS process works. Set:

- `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET` — CDP facilitator credentials
- `X402_SELLER_PAY_TO_ADDRESS` — the EVM address to receive payment
- `X402_RESOURCE_URL` — the public HTTPS URL for `POST /invoke` (used in the
  x402 Bazaar discovery listing and route config)
- `PORT` — injected by most hosting platforms

Then run `python3 service.py` (no CLI args — this is the production shape:
binds `0.0.0.0`, reads `$PORT`).
