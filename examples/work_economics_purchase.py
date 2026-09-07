"""Buy an Inferrail Work Economics summary — Base Sepolia TESTNET only.

A complete, standalone x402 buyer. Uses YOUR OWN Base Sepolia wallet's
private key to sign a real (testnet) payment authorization — no Inferrail
account, no CDP account, and no prior relationship with Inferrail is
required. See docs/capabilities/work-economics.md for the full contract.

This is testnet-only. Base Sepolia test-USDC has no real monetary value.

Install:
    pip install "x402[evm]" httpx

Run:
    TESTER_PRIVATE_KEY=0xYOUR_TESTNET_PRIVATE_KEY python3 work_economics_purchase.py

Optional: pass a different endpoint as the first CLI argument.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import httpx
from eth_account import Account
from x402 import x402Client
from x402.http import decode_payment_response_header
from x402.http.x402_http_client import x402HTTPClient
from x402.mechanisms.evm.exact import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner

NETWORK = "eip155:84532"  # Base Sepolia (CAIP-2)
DEFAULT_ENDPOINT = "https://work.tryinferrail.com/invoke"

# A valid example request body matching the schema published at
# docs/capabilities/work-economics.md and GET <base_url>/manifest.
EXAMPLE_BODY: dict = {
    "work_id": "example-buyer-work-001",
    "events": [
        {
            "resource_class": "inference",
            "supplier": "example-supplier",
            "known_cost_usd": "0.02",
            "price_basis": "LIST_PRICE_PUBLISHED",
            "currency": "USD",
            "status": "success",
        }
    ],
    "outcome_status": "success",
}


async def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ENDPOINT

    private_key = os.environ.get("TESTER_PRIVATE_KEY")
    if not private_key:
        raise SystemExit(
            "Set TESTER_PRIVATE_KEY to your own Base Sepolia TESTNET wallet's "
            "private key (never share this key, never commit it anywhere)."
        )

    account = Account.from_key(private_key)
    print(f"Buyer address: {account.address}")

    purchase_id = f"work-economics-{uuid.uuid4()}"
    print(f"Purchase id (do not reuse for a different attempt): {purchase_id}")

    # --- Step 1: unpaid call. Expect HTTP 402. ---
    unpaid = httpx.post(
        base_url, headers={"X-Purchase-Id": purchase_id}, json=EXAMPLE_BODY, timeout=15.0
    )
    print(f"[1] Unpaid status: {unpaid.status_code}")
    if unpaid.status_code != 402:
        print("Expected HTTP 402. Response body:")
        print(unpaid.text)
        return

    # The x402 SDK accepts PaymentRequirements from either the response body
    # or a Payment-Required header -- this server sends it via the header.
    signer = EthAccountSigner(account)
    client = x402Client()
    register_exact_evm_client(client, signer, networks=NETWORK)
    http_client = x402HTTPClient(client)

    payment_required = http_client.get_payment_required_response(
        lambda name: unpaid.headers.get(name), unpaid.json() if unpaid.text else None
    )
    accepted = payment_required.accepts[0]
    print(
        f"[2] Payment required: {accepted.amount} atomic units of "
        f"{accepted.asset} on {accepted.network}, pay to {accepted.pay_to}"
    )

    # --- Step 2: sign the payment with YOUR OWN key. ---
    payment_payload = await client.create_payment_payload(payment_required)
    payment_headers = http_client.encode_payment_signature_header(payment_payload)

    # --- Step 3: retry with the SAME purchase_id and the signed payment header. ---
    paid = httpx.post(
        base_url,
        headers={"X-Purchase-Id": purchase_id, **payment_headers},
        json=EXAMPLE_BODY,
        timeout=30.0,
    )
    print(f"[3] Paid status: {paid.status_code}")

    settle_header = paid.headers.get("PAYMENT-RESPONSE") or paid.headers.get("X-PAYMENT-RESPONSE")
    if settle_header:
        settle = decode_payment_response_header(settle_header)
        print(f"[4] Settlement success: {settle.success}")
        print(f"[4] Transaction hash: {settle.transaction}")
        print(f"[4] Network: {settle.network}")
    else:
        print("[4] No PAYMENT-RESPONSE header returned (settlement may be asynchronous).")

    print("[5] Result + commercial receipt:")
    print(paid.json())


if __name__ == "__main__":
    asyncio.run(main())
