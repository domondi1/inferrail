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


# -- refunds: proof that USDC actually went back -----------------------------------------------
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class RefundProofError(ValueError):
    """The transaction does not prove the refund (wrong token, parties, status, or too new)."""


def _rpc(rpc_url: str, method: str, params: list[object], client: httpx.Client | None) -> object:
    own = client is None
    c = client or httpx.Client(timeout=20)
    try:
        out = c.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        return out.json().get("result")
    finally:
        if own:
            c.close()


def _topic_address(topic: str) -> str:
    return "0x" + topic.lower().removeprefix("0x")[-40:]


def usdc_refund_cents(
    rpc_url: str,
    network: str,
    tx_hash: str,
    sender: str,
    recipient: str,
    *,
    min_confirmations: int = 3,
    client: httpx.Client | None = None,
) -> int:
    """Whole cents of USDC that transaction `tx_hash` moved from `sender` (our receiving
    address) to `recipient` (the original payer), read from the chain.

    Raises RefundProofError when the transaction doesn't prove a refund: not found, reverted,
    fewer than `min_confirmations` blocks deep, or no USDC Transfer between those two
    addresses. Raises httpx.HTTPError when the chain can't be read (try again later)."""
    if not (tx_hash.startswith("0x") and len(tx_hash) == 66):
        raise RefundProofError("tx hash must be 0x + 64 hex characters")
    receipt = _rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash], client)
    if not isinstance(receipt, dict):
        raise RefundProofError("transaction not found on this network")
    if receipt.get("status") != "0x1":
        raise RefundProofError("transaction reverted")
    latest = _rpc(rpc_url, "eth_blockNumber", [], client)
    depth = int(str(latest), 16) - int(str(receipt["blockNumber"]), 16) + 1
    if depth < min_confirmations:
        raise RefundProofError(f"only {depth} confirmations; wait for {min_confirmations}")
    usdc, sender, recipient = USDC[network].lower(), sender.lower(), recipient.lower()
    units = 0
    for log in receipt.get("logs") or []:
        topics = [str(t).lower() for t in log.get("topics") or []]
        if (
            str(log.get("address", "")).lower() == usdc
            and len(topics) == 3
            and topics[0] == TRANSFER_TOPIC
            and _topic_address(topics[1]) == sender
            and _topic_address(topics[2]) == recipient
        ):
            units += int(str(log.get("data") or "0x0"), 16)
    if units == 0:
        raise RefundProofError("no USDC transfer from the receiving address to the payer")
    return units // 10_000  # USDC has 6 decimals: 10,000 units = 1 cent
