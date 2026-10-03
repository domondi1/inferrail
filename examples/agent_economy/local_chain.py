"""A local stand-in for Base Sepolia USDC, plus an x402 facilitator over it.

Only the chain is simulated. Signatures are checked with x402's own
EIP-3009 verification (`classify_eip3009_signature`, the same code a real
facilitator runs), and the transfer rules are USDC's documented
`transferWithAuthorization` rules:

- the signature must recover to `from`;
- `validAfter < now < validBefore`;
- each `(from, nonce)` can be used once, ever (`authorizationState`);
- `from` must hold at least `value`.

A successful transfer moves the balance, marks the nonce used, and emits
an `AuthorizationUsed` event with a transaction hash. That is everything
the buyer's reconciler reads, so reconciliation code written against this
class runs unchanged against the real chain (see `BaseSepoliaReader` in
`authority.py`).
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from x402.mechanisms.evm.exact.eip3009_utils import classify_eip3009_signature
from x402.mechanisms.evm.types import ExactEIP3009Authorization
from x402.schemas import PaymentPayload, PaymentRequirements
from x402.schemas.responses import (
    SettleResponse,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
)

NETWORK = "eip155:84532"
CHAIN_ID = 84532
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


class _EoaOnly:
    """The one chain read x402's signature check needs: no address has code."""

    def get_code(self, address: str) -> bytes:
        return b""


@dataclass
class AuthorizationUsed:
    authorizer: str
    nonce: str
    to: str
    value: int
    tx: str


@dataclass
class LocalUsdcChain:
    clock: Callable[[], float] = time.time
    balances: dict[str, int] = field(default_factory=dict)
    events: list[AuthorizationUsed] = field(default_factory=list)
    _used: dict[tuple[str, str], AuthorizationUsed] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def mint(self, address: str, atomic: int) -> None:
        with self._lock:
            key = address.lower()
            self.balances[key] = self.balances.get(key, 0) + atomic

    def balance_of(self, address: str) -> int:
        return self.balances.get(address.lower(), 0)

    def authorization_state(self, authorizer: str, nonce: str) -> bool:
        return (authorizer.lower(), nonce.lower()) in self._used

    def used_event(self, authorizer: str, nonce: str) -> AuthorizationUsed | None:
        return self._used.get((authorizer.lower(), nonce.lower()))

    def check(self, payload: PaymentPayload, requirements: PaymentRequirements) -> str | None:
        """Returns the reason this transfer would revert, or None."""
        auth = payload.payload["authorization"]
        authorization = ExactEIP3009Authorization(
            from_address=auth["from"],
            to=auth["to"],
            value=auth["value"],
            valid_after=auth["validAfter"],
            valid_before=auth["validBefore"],
            nonce=auth["nonce"],
        )
        signature = bytes.fromhex(payload.payload["signature"].removeprefix("0x"))
        extra = requirements.extra or {}
        verdict = classify_eip3009_signature(
            _EoaOnly(),
            authorization,
            signature,
            CHAIN_ID,
            requirements.asset,
            extra.get("name", "USDC"),
            extra.get("version", "2"),
        )
        if not verdict.valid:
            return "invalid_signature"
        if auth["to"].lower() != requirements.pay_to.lower():
            return "recipient_mismatch"
        if int(auth["value"]) != int(requirements.amount):
            return "amount_mismatch"
        now = self.clock()
        if not int(auth["validAfter"]) < now < int(auth["validBefore"]):
            return "authorization_not_currently_valid"
        if self.authorization_state(auth["from"], auth["nonce"]):
            return "nonce_already_used"
        if self.balance_of(auth["from"]) < int(auth["value"]):
            return "insufficient_balance"
        return None

    def transfer_with_authorization(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> AuthorizationUsed:
        with self._lock:
            reason = self.check(payload, requirements)
            if reason is not None:
                raise ValueError(reason)
            auth = payload.payload["authorization"]
            sender, receiver, value = auth["from"].lower(), auth["to"].lower(), int(auth["value"])
            self.balances[sender] -= value
            self.balances[receiver] = self.balances.get(receiver, 0) + value
            tx = "0x" + hashlib.sha256(f"{sender}:{auth['nonce']}".encode()).hexdigest()
            event = AuthorizationUsed(sender, auth["nonce"].lower(), receiver, value, tx)
            self._used[(sender, auth["nonce"].lower())] = event
            self.events.append(event)
            return event


@dataclass
class LocalFacilitator:
    """x402's async FacilitatorClient protocol, settling on `LocalUsdcChain`."""

    chain: LocalUsdcChain
    before_settle: Callable[[PaymentPayload], None] | None = None

    def get_supported(self) -> SupportedResponse:
        return SupportedResponse(
            kinds=[SupportedKind(x402_version=2, scheme="exact", network=NETWORK)]
        )

    async def verify(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> VerifyResponse:
        payer = payload.payload["authorization"]["from"]
        reason = self.chain.check(payload, requirements)
        return VerifyResponse(is_valid=reason is None, invalid_reason=reason, payer=payer)

    async def settle(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> SettleResponse:
        payer = payload.payload["authorization"]["from"]
        if self.before_settle is not None:
            self.before_settle(payload)
        try:
            event = self.chain.transfer_with_authorization(payload, requirements)
        except ValueError as exc:
            return SettleResponse(
                success=False, error_reason=str(exc), transaction="", network=NETWORK, payer=payer
            )
        return SettleResponse(
            success=True,
            transaction=event.tx,
            network=NETWORK,
            payer=payer,
            amount=str(event.value),
        )


def describe(event: Any) -> dict[str, Any]:
    return {"tx": event.tx, "from": event.authorizer, "to": event.to, "atomic": event.value}
