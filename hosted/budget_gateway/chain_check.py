"""On-chain truth for pending x402 credit purchases.

USDC implements EIP-3009 `authorizationState(authorizer, nonce) -> bool`: true once a transfer
authorization has been used, i.e. the payment settled. This is how a purchase that settled
while the service crashed (before its after-settle hook ran) is still credited. It is checked
against the chain itself, not against our own records.
"""

from __future__ import annotations

import httpx

SELECTOR = "0xe94a0102"  # keccak256("authorizationState(address,bytes32)")[:4]
USDC = {
    "eip155:8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "eip155:84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}


def calldata(authorizer: str, nonce: str) -> str:
    addr = authorizer.lower().removeprefix("0x").rjust(64, "0")
    n = nonce.lower().removeprefix("0x").rjust(64, "0")
    if len(addr) != 64 or len(n) != 64:
        raise ValueError("authorizer must be an address and nonce 32 bytes")
    return SELECTOR + addr + n


def authorization_used(
    rpc_url: str, network: str, authorizer: str, nonce: str, client: httpx.Client | None = None
) -> bool | None:
    """True/False from the chain, or None when the answer is unknown (RPC failure)."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": USDC[network], "data": calldata(authorizer, nonce)}, "latest"],
    }
    try:
        own = client is None
        c = client or httpx.Client(timeout=20)
        try:
            result = c.post(rpc_url, json=body).json().get("result")
        finally:
            if own:
                c.close()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(result, str) or not result.startswith("0x") or len(result) != 66:
        return None
    return int(result, 16) != 0
