"""Credential-free, network-free x402 facilitator for hosted payment tests.

Everything EXCEPT the facilitator is real: the installed x402 2.22.0
`x402ResourceServer`, its FastAPI `payment_middleware`, payment-flow
resolution, hook dispatch, and buyer-side signing (a throwaway
`eth_account` key signs a real EIP-3009 authorization via the real
`x402ClientSync`/`ExactEvmScheme`, entirely offline). Only the three
network calls a real facilitator would make -- `get_supported`, `verify`,
`settle` -- are replaced, so every paid-path test is deterministic and
needs no CDP credentials, funded wallet, or network access.

The fake deliberately does NOT try to reproduce CDP's facilitator. Its
outcomes are scripted per test. The one piece of chain behavior it models
(opt-out via `model_single_use_nonces=False`) is that an EIP-3009 nonce,
once settled, can never be verified or settled again -- a property of the
USDC contract itself, not of any facilitator. Tests that depend on that
model say so.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from eth_account import Account
from x402.client import x402ClientSync
from x402.http.utils import decode_payment_signature_header, encode_payment_signature_header
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.schemas import PaymentPayload, PaymentRequired, PaymentRequirements
from x402.schemas.responses import (
    SettleResponse,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
)

BASE_SEPOLIA = "eip155:84532"

# A settle outcome is either a SettleResponse to return, an exception to
# raise (modeling a transport failure / non-200 / timeout), or a callable
# producing either.
SettleOutcome = SettleResponse | BaseException | Callable[..., Any]


def authorization_of(payload: PaymentPayload) -> dict[str, Any]:
    """The EIP-3009 authorization dict from a V2 exact-scheme payload."""
    authorization = payload.payload["authorization"]
    assert isinstance(authorization, dict)
    return authorization


def nonce_of(payload: PaymentPayload) -> str:
    return str(authorization_of(payload)["nonce"])


def payer_of(payload: PaymentPayload) -> str:
    return str(authorization_of(payload)["from"])


@dataclass
class FacilitatorCall:
    method: str  # "verify" | "settle"
    nonce: str
    payer: str


@dataclass
class FakeFacilitator:
    """Implements x402's async `FacilitatorClient` protocol
    (`x402.server_base.FacilitatorClient`).

    `settle_outcomes` is consumed one per `settle()` call; when exhausted,
    settlement succeeds. `on_settle` runs inside `settle()` before the
    outcome is produced, so a test can observe durable state at the exact
    moment money would move.
    """

    network: str = BASE_SEPOLIA
    settle_outcomes: list[SettleOutcome] = field(default_factory=list)
    verify_invalid_reason: str | None = None
    model_single_use_nonces: bool = True
    on_settle: Callable[[PaymentPayload], None | Awaitable[None]] | None = None
    calls: list[FacilitatorCall] = field(default_factory=list)
    settled_nonces: set[tuple[str, str]] = field(default_factory=set)

    def get_supported(self) -> SupportedResponse:
        return SupportedResponse(
            kinds=[SupportedKind(x402_version=2, scheme="exact", network=self.network)]
        )

    def calls_to(self, method: str) -> list[FacilitatorCall]:
        return [c for c in self.calls if c.method == method]

    def _key(self, payload: PaymentPayload) -> tuple[str, str]:
        return (payer_of(payload).lower(), nonce_of(payload).lower())

    async def verify(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> VerifyResponse:
        self.calls.append(FacilitatorCall("verify", nonce_of(payload), payer_of(payload)))
        if self.model_single_use_nonces and self._key(payload) in self.settled_nonces:
            return VerifyResponse(
                is_valid=False,
                invalid_reason="invalid_exact_evm_nonce_already_used",
                payer=payer_of(payload),
            )
        if self.verify_invalid_reason is not None:
            return VerifyResponse(
                is_valid=False, invalid_reason=self.verify_invalid_reason, payer=payer_of(payload)
            )
        return VerifyResponse(is_valid=True, payer=payer_of(payload))

    async def settle(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> SettleResponse:
        self.calls.append(FacilitatorCall("settle", nonce_of(payload), payer_of(payload)))
        if self.on_settle is not None:
            maybe = self.on_settle(payload)
            if maybe is not None:
                await maybe
        outcome: SettleOutcome | None = (
            self.settle_outcomes.pop(0) if self.settle_outcomes else None
        )
        if callable(outcome) and not isinstance(outcome, BaseException):
            outcome = outcome(payload, requirements)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            if self.model_single_use_nonces and self._key(payload) in self.settled_nonces:
                return SettleResponse(
                    success=False,
                    error_reason="invalid_exact_evm_nonce_already_used",
                    transaction="",
                    network=requirements.network,
                    payer=payer_of(payload),
                )
            outcome = settle_success(payload, requirements)
        if outcome.success:
            self.settled_nonces.add(self._key(payload))
        return outcome


def settle_success(payload: PaymentPayload, requirements: PaymentRequirements) -> SettleResponse:
    """A successful settlement with a deterministic, per-nonce fake tx hash.

    Derived by hashing, so the tx hash never appears verbatim inside the
    signed payload itself (which contains the nonce).
    """
    digest = hashlib.sha256(b"fake-settlement-tx:" + nonce_of(payload).encode()).hexdigest()
    return SettleResponse(
        success=True,
        transaction="0x" + digest,
        network=requirements.network,
        payer=payer_of(payload),
        amount=requirements.amount,
    )


def settle_failure(reason: str = "invalid_exact_evm_insufficient_balance") -> Callable[..., Any]:
    def _failure(payload: PaymentPayload, requirements: PaymentRequirements) -> SettleResponse:
        return SettleResponse(
            success=False,
            error_reason=reason,
            error_message="injected test failure",
            transaction="",
            network=requirements.network,
            payer=payer_of(payload),
        )

    return _failure


def decode_payment_required(response: Any) -> PaymentRequired:
    """Parses the `PAYMENT-REQUIRED` header of an unpaid 402 response."""
    encoded = response.headers["payment-required"]
    return PaymentRequired.model_validate(json.loads(base64.b64decode(encoded)))


@dataclass
class SignedPayment:
    header: str
    payer: str
    nonce: str
    payload: PaymentPayload


def sign_payment(payment_required: PaymentRequired, account: Any = None) -> SignedPayment:
    """Signs a real EIP-3009 authorization for the server's own advertised
    requirements, exactly as a buyer's x402 client would -- offline, with a
    throwaway (unfunded) key unless one is supplied."""
    account = account if account is not None else Account.create()
    buyer = x402ClientSync()
    buyer.register(BASE_SEPOLIA, ExactEvmScheme(signer=account))
    payload = buyer.create_payment_payload(payment_required)
    header = encode_payment_signature_header(payload)
    decoded = decode_payment_signature_header(header)
    assert isinstance(decoded, PaymentPayload)
    return SignedPayment(
        header=header, payer=account.address, nonce=nonce_of(decoded), payload=decoded
    )
