"""x402 exact settled before supplier, with independent USDC event evidence."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak
from x402.mechanisms.evm.eip712 import build_typed_data_for_signing
from x402.mechanisms.evm.types import ExactEIP3009Authorization
from x402.schemas import PaymentPayload, PaymentRequirements

TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
USED = "0x" + keccak(text="AuthorizationUsed(address,bytes32)").hex()


def encode(value: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def decode(value: str) -> PaymentPayload:
    if len(value) > 32_768:
        raise ValueError("payment_too_large")
    return PaymentPayload.model_validate(json.loads(base64.b64decode(value, validate=True)))


def identity(payload: PaymentPayload, requirements: PaymentRequirements) -> tuple[str, str]:
    """Authenticate replay without asking a facilitator to revalidate a spent nonce.

    V0 explicitly accepts EOA TransferWithAuthorization, not smart accounts or Permit2.
    Local recovery is essential: /verify rejects an already settled authorization.
    """
    if payload.x402_version != 2 or payload.accepted != requirements:
        raise ValueError("payment_requirements_mismatch")
    raw = payload.payload["authorization"]
    if raw["to"].lower() != requirements.pay_to.lower() or int(raw["value"]) != int(
        requirements.amount
    ):
        raise ValueError("authorization_mismatch")
    if len(bytes.fromhex(raw["nonce"].removeprefix("0x"))) != 32:
        raise ValueError("invalid_nonce")
    sig = bytes.fromhex(payload.payload["signature"].removeprefix("0x"))
    if len(sig) != 65:
        raise ValueError("EOA_EIP3009_only")
    authorization = ExactEIP3009Authorization(
        from_address=raw["from"],
        to=raw["to"],
        value=str(raw["value"]),
        valid_after=str(raw["validAfter"]),
        valid_before=str(raw["validBefore"]),
        nonce=raw["nonce"],
    )
    domain, types, primary, message = build_typed_data_for_signing(
        authorization,
        int(str(requirements.network).split(":")[1]),
        requirements.asset,
        requirements.extra["name"],
        requirements.extra["version"],
    )
    signing = encode_typed_data(
        full_message={
            "domain": {
                "name": domain.name,
                "version": domain.version,
                "chainId": domain.chain_id,
                "verifyingContract": domain.verifying_contract,
            },
            "types": types,
            "primaryType": primary,
            "message": message,
        }
    )
    payer = Account.recover_message(signing, signature=sig).lower()
    if payer != raw["from"].lower():
        raise ValueError("invalid_signature")
    return payer, raw["nonce"].lower()


class ChainEvidence:
    def __init__(self, rpc_url: str, requirements: PaymentRequirements, *, finalized: bool = True):
        self.rpc_url, self.requirements, self.finalized = rpc_url, requirements, finalized
        self.client = httpx.AsyncClient(timeout=20)

    async def rpc(self, method: str, params: list[Any]) -> Any:
        response = await self.client.post(
            self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise RuntimeError("chain_evidence_unavailable")
        return body["result"]

    async def confirmed(self, payload: PaymentPayload, transaction: str) -> bool:
        req = self.requirements
        if int(await self.rpc("eth_chainId", []), 16) != int(str(req.network).split(":")[1]):
            raise RuntimeError("wrong_chain")
        receipt = await self.rpc("eth_getTransactionReceipt", [transaction])
        if not receipt or receipt.get("status") != "0x1":
            return False
        head = await self.rpc(
            "eth_getBlockByNumber", ["finalized" if self.finalized else "latest", False]
        )
        if not head or int(head["number"], 16) < int(receipt["blockNumber"], 16):
            return False
        block = await self.rpc("eth_getBlockByNumber", [receipt["blockNumber"], False])
        if not block or block["hash"].lower() != receipt["blockHash"].lower():
            return False
        auth = payload.payload["authorization"]
        payer, nonce = auth["from"].lower(), auth["nonce"].lower()
        transfers, used = False, False
        for log in receipt.get("logs", []):
            if log["address"].lower() != req.asset.lower() or log.get("removed", False):
                continue
            topics = [topic.lower() for topic in log["topics"]]
            if len(topics) == 3 and topics[0] == TRANSFER:
                transfers |= (
                    "0x" + topics[1][-40:] == payer
                    and "0x" + topics[2][-40:] == req.pay_to.lower()
                    and int(log["data"], 16) == int(req.amount)
                )
            if len(topics) >= 2 and topics[0] == USED:
                event_nonce = topics[2] if len(topics) == 3 else log["data"]
                used |= "0x" + topics[1][-40:] == payer and event_nonce.lower() == nonce
        return transfers and used

    async def find_transaction(self, payload: PaymentPayload, from_block: str) -> str | None:
        """Read-only crash recovery; never resubmit a payment whose outcome is unknown."""
        auth = payload.payload["authorization"]
        logs = await self.rpc(
            "eth_getLogs",
            [
                {
                    "address": self.requirements.asset,
                    "fromBlock": from_block,
                    "toBlock": "latest",
                    "topics": [USED, "0x" + auth["from"].removeprefix("0x").lower().zfill(64)],
                }
            ],
        )
        for log in logs:
            topics = log["topics"]
            event_nonce = topics[2] if len(topics) == 3 else log["data"]
            if event_nonce.lower() == auth["nonce"].lower() and not log.get("removed", False):
                return str(log["transactionHash"])
        return None
