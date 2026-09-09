"""Inferrail Economic Authority — paid session creation (Phase C).

A buyer purchases an **Inferrail Economic Authority session**: a durable
coordination boundary (a root delegation in `core.py`) that lets agents
operate under a shared, buyer-declared spending ceiling without
double-allocation. The x402 payment that opens a session is **Inferrail's
service fee for creating and hosting this coordination boundary** -- it
is never represented as depositing, funding, or escrowing the delegated
ceiling itself. Inferrail takes no custody of `authority_ceiling_usd`;
that number is caller-declared accounting/policy metadata enforced by
`core.py`, exactly as it already was in Phase A/B.

This module is transport-independent, like `core.py` and `capabilities.py`
-- it knows nothing about x402, FastAPI, or HTTP. `server.py` is
responsible for running the real x402 payment-verification handshake and
extracting a `payment_nonce` (the payment authorization's own nonce, a
non-secret, single-use identifier) plus the buyer's requested
`agent_id`/`authority_ceiling_usd` from the verified request, then
calling `create_or_recover_session` here exactly once per HTTP request.

## Settlement-before-handler (payment-security repair)

`server.py` configures the `/sessions` route with x402's official
`"upfront"` payment flow (`extra={"paymentFlow": "upfront"}` on the
route's `PaymentOption`, supported by the installed `eip3009` asset
transfer method -- see `x402.payment_flow.PAYMENT_FLOWS`), instead of the
scheme's default `"authorization"` flow. Concretely, this means x402
completes real on-chain settlement BEFORE this module's
`create_or_recover_session` ever runs, not after: `x402ResourceServer`
only calls into the FastAPI route handler once `process_http_request`
has already returned a completed `before_handler_settlement`, and
returns a 402 directly -- without ever calling the handler -- if
settlement fails. This eliminates, structurally rather than by
convention, an earlier defect in this module where a session's root
credential was minted and its `service_fee.status` reported `"PAID"`
before x402 settlement had been attempted at all (settlement ran only
after the handler returned, under the scheme's default flow) -- so a
settlement failure after a successful verification could leave a
real, `"PAID"`-labeled session and an unclaimed root credential for a
payment that never actually landed. Under the `"upfront"` flow, by the
time any code in this module runs, the buyer's USDC has already, truly
moved; there is no remaining code path where this module reports `PAID`
and settlement then fails, because there is no "then" left -- settlement
already happened.

Idempotency and crash-safety: `capabilities.record_session_purchase` and
`capabilities.issue_or_rotate_session_credential` together give session
creation the exact same guarantees Phase B's `reserve` flow already has
for its own credential issuance (see that module's docstrings) --
`payment_nonce` (the real, on-chain, verified EIP-3009 nonce) remains the
sole AUTHORITATIVE idempotency key, `session_id` is always
server-generated (never caller-supplied, so it can never be raced or
guessed into pointing at someone else's payment), and at most one root
credential for a session is ever live. `core.create_root` is itself
idempotent per `delegation_id`, so calling it on every retry (whether or
not this is the payment's first successful processing) never creates a
second root or changes an already-recorded ceiling.

`payment_identifier` (the buyer-chosen x402 payment-identifier extension
value, when the buyer supplies one) is recorded alongside the purchase
for correlation/audit purposes only -- it is deliberately NOT used as an
alternative idempotency key or trust boundary. Unlike `payment_nonce`, an
`id` is buyer-chosen and never cryptographically bound to the actual
on-chain transfer; treating it as authoritative would let a buyer's own
reused or coincidentally-colliding `id` silently absorb a second, real,
distinct payment into the first purchase's session (the buyer pays
twice, receives nothing new) -- a real loss for the buyer that the
authoritative-nonce design never risks. This is why `payment_nonce`,
not `payment_identifier`, remains the sole key `record_session_purchase`
enforces conflicts against.

Root credential exposure follows the same "exposed at most once, then
never again" discipline the rest of this service uses for capability
tokens: the plaintext is returned to the caller only on the transaction
that actually mints it. A payment nonce that has already been
successfully claimed can never mint or expose a replacement through the
ordinary purchase path. Recovery for a buyer who was genuinely charged
but never received (or has since lost) the credential is handled by a
SEPARATE, deliberately minimal mechanism: `capabilities.
recover_session_credential`, driven by a `recovery_secret_hash` the
buyer may optionally include on the original `POST /sessions` request --
a SHA-256 commitment over a secret the buyer generates and keeps for
themselves, so recovery never depends on the buyer having successfully
received anything from this service in the first place (unlike a
server-minted recovery token, which would share the exact same
loss-of-response risk as the credential it exists to recover). See that
function's docstring for the full guarantee and for why this is the
smallest safe design available, given Phase C has no pre-existing
capability token to check an identity against the way Phase B's
`reserve` does (`authorizing_token_id`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from capabilities import (
    SCOPES,
    CapabilityStore,
    InvalidRecoverySecret,
    SessionAuthorizationConflict,
)
from core import EconomicAuthorityStore, validate_amount, validate_identifier

MAX_PAYMENT_NONCE_LENGTH = 256
MAX_PAYMENT_IDENTIFIER_LENGTH = 128

# A buyer-supplied `recovery_secret_hash` must be the hex-encoded SHA-256
# of a secret the buyer generated and kept for themselves -- see
# `capabilities.CapabilityStore.recover_session_credential`'s docstring
# for why this is the smallest safe recovery design available (the
# secret survives even a total loss of every response this service ever
# sends, since the buyer never depends on receiving it from us). This
# service only ever sees and stores the hash, never the plaintext.
_RECOVERY_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _canonical_decimal_str(value: Decimal) -> str:
    """Canonical string form of a Decimal amount, matching
    `core._canonical_decimal_str` exactly (duplicated here, not imported,
    to keep this module's only dependency on `core.py` its public
    validators/store -- see that function's own docstring for why plain
    `Decimal.normalize()` alone is not enough)."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True)
class SessionCreationResult:
    """Result of one `create_or_recover_session` call. `plaintext_token`
    is the root capability's plaintext, present only on the transaction
    that actually mints it -- `None` whenever this payment's session was
    already claimed by an earlier call (ordinary duplicate delivery)."""

    session_id: str
    agent_id: str
    authority_ceiling_usd: Decimal
    service_fee_usd: Decimal
    plaintext_token: str | None
    newly_claimed: bool


def create_or_recover_session(
    core: EconomicAuthorityStore,
    capabilities: CapabilityStore,
    *,
    payment_nonce: str,
    agent_id: str,
    authority_ceiling_usd: Decimal,
    service_fee_usd: Decimal,
    payment_identifier: str | None = None,
    recovery_secret_hash: str | None = None,
) -> SessionCreationResult:
    """Creates (or safely re-observes) exactly one session for a single
    verified payment. Safe to call multiple times, concurrently or
    sequentially, for the same `payment_nonce` -- see this module's
    docstring for the full guarantee. Raises `ValueError` for malformed
    input and `capabilities.SessionAuthorizationConflict` if
    `payment_nonce` is already bound to a different `agent_id`,
    `authority_ceiling_usd`, or `service_fee_usd` (one payment proof can
    never purchase two unrelated sessions).

    `payment_identifier` is the optional x402 payment-identifier
    extension value (recorded for audit/correlation only -- see this
    module's docstring for why it is never treated as a trust boundary).
    `recovery_secret_hash`, if given, must be a 64-character lowercase
    hex SHA-256 digest the buyer computed over a secret only they hold;
    it is recorded so a later `capabilities.recover_session_credential`
    call can verify that secret without this service ever seeing or
    storing its plaintext.
    """
    validate_identifier("payment_nonce", payment_nonce)
    if len(payment_nonce) > MAX_PAYMENT_NONCE_LENGTH:
        raise ValueError(f"payment_nonce must be at most {MAX_PAYMENT_NONCE_LENGTH} characters")
    validate_identifier("agent_id", agent_id)
    validate_amount(authority_ceiling_usd, name="authority_ceiling_usd")
    validate_amount(service_fee_usd, name="service_fee_usd")
    if authority_ceiling_usd <= 0:
        raise ValueError("authority_ceiling_usd must be a positive amount")
    if service_fee_usd <= 0:
        raise ValueError("service_fee_usd must be a positive amount")
    if payment_identifier is not None:
        if not isinstance(payment_identifier, str) or not payment_identifier:
            raise ValueError("payment_identifier must be a non-empty string")
        if len(payment_identifier) > MAX_PAYMENT_IDENTIFIER_LENGTH:
            raise ValueError(
                f"payment_identifier must be at most {MAX_PAYMENT_IDENTIFIER_LENGTH} characters"
            )
    if recovery_secret_hash is not None and not _RECOVERY_HASH_PATTERN.match(
        recovery_secret_hash
    ):
        raise ValueError("recovery_secret_hash must be a 64-character lowercase hex SHA-256 digest")

    authority_str = _canonical_decimal_str(authority_ceiling_usd)
    fee_str = _canonical_decimal_str(service_fee_usd)

    purchase = capabilities.record_session_purchase(
        payment_nonce,
        agent_id,
        authority_str,
        fee_str,
        payment_identifier=payment_identifier,
        recovery_secret_hash=recovery_secret_hash,
    )

    # core.create_root is idempotent per delegation_id -- safe on every
    # call regardless of whether this payment_nonce is brand new or a
    # retry. Uses the DURABLY RECORDED agent_id/ceiling (not whatever
    # this particular call was passed), so a retry can never drift the
    # root's own economic state even if record_session_purchase already
    # rejected a mismatched retry above (defense in depth, not reachable
    # in practice since a mismatch already raised).
    core.create_root(
        event_id=f"session-root:{payment_nonce}",
        delegation_id=purchase.session_id,
        agent_id=purchase.agent_id,
        envelope_usd=Decimal(purchase.authority_ceiling_usd),
    )

    minted = capabilities.issue_or_rotate_session_credential(
        payment_nonce, purchase.session_id, SCOPES
    )
    if minted is None:
        return SessionCreationResult(
            session_id=purchase.session_id,
            agent_id=purchase.agent_id,
            authority_ceiling_usd=Decimal(purchase.authority_ceiling_usd),
            service_fee_usd=Decimal(purchase.service_fee_usd),
            plaintext_token=None,
            newly_claimed=False,
        )
    _token_id, plaintext = minted
    return SessionCreationResult(
        session_id=purchase.session_id,
        agent_id=purchase.agent_id,
        authority_ceiling_usd=Decimal(purchase.authority_ceiling_usd),
        service_fee_usd=Decimal(purchase.service_fee_usd),
        plaintext_token=plaintext,
        newly_claimed=True,
    )


_SESSION_CURRENCY = "USD"


def handle_session_request(
    core: EconomicAuthorityStore,
    capabilities: CapabilityStore,
    *,
    body: object,
    payment_nonce: str,
    service_fee_usd: Decimal,
    payment_identifier: str | None = None,
) -> tuple[int, dict[str, object]]:
    """Transport-independent core of the `POST /sessions` handler: given
    an ALREADY-VERIFIED payment's nonce and the raw (already
    JSON-decoded) request body, validates the buyer's requested
    `agent_id`/`authority_ceiling_usd`, calls `create_or_recover_session`,
    and returns `(http_status_code, response_body)`.

    Kept free of FastAPI/x402 so it can be tested directly, exhaustively,
    and without any facilitator/network dependency -- exactly like every
    other business-logic function in this hosted module. `server.py`'s
    route is a thin wrapper: parse the request body as JSON, obtain
    `payment_nonce` (and, since the repair, the payment-identifier
    extension value if the buyer sent one) from the x402 middleware's
    verified payment payload, call this function, and translate the
    result into a `JSONResponse`. By the time `server.py` can even call
    this function, the payment-flow fix means x402 settlement has
    ALREADY succeeded on-chain -- see `server.py`'s module docstring --
    so every response this function returns for a fresh purchase is
    honestly "PAID": there is no code path left in which this function
    runs, mints a session, and settlement then fails.

    `body` may include an optional `recovery_secret_hash`: the
    hex-encoded SHA-256 of a secret the buyer generated and kept for
    themselves. If given, it is durably recorded (once, at first
    purchase) so a later `POST /sessions/recover` call can prove
    ownership of this session without ever sending us the plaintext
    secret in a request we might fail to answer -- see
    `capabilities.CapabilityStore.recover_session_credential`.
    """
    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}

    agent_id = body.get("agent_id")
    raw_ceiling = body.get("authority_ceiling_usd")
    recovery_secret_hash = body.get("recovery_secret_hash")
    if not isinstance(agent_id, str) or not agent_id:
        return 400, {"error": "agent_id is required"}
    if raw_ceiling is None:
        return 400, {"error": "authority_ceiling_usd is required"}
    if recovery_secret_hash is not None and not isinstance(recovery_secret_hash, str):
        return 400, {"error": "recovery_secret_hash must be a string"}
    try:
        authority_ceiling_usd = Decimal(str(raw_ceiling))
    except InvalidOperation:
        return 400, {"error": "authority_ceiling_usd must be a decimal amount"}

    try:
        result = create_or_recover_session(
            core,
            capabilities,
            payment_nonce=payment_nonce,
            agent_id=agent_id,
            authority_ceiling_usd=authority_ceiling_usd,
            service_fee_usd=service_fee_usd,
            payment_identifier=payment_identifier,
            recovery_secret_hash=recovery_secret_hash,
        )
    except SessionAuthorizationConflict as exc:
        return 409, {"error": "SessionAuthorizationConflict", "detail": str(exc)}
    except ValueError as exc:
        return 400, {"error": "invalid request", "detail": str(exc)}

    response_body: dict[str, object] = {
        "session_id": result.session_id,
        "agent_id": result.agent_id,
        "authority_ceiling_usd": str(result.authority_ceiling_usd),
        "service_fee": {
            "amount_usd": str(result.service_fee_usd),
            "currency": _SESSION_CURRENCY,
            "status": "PAID",
        },
        "root_capability": (
            {
                "token": result.plaintext_token,
                "scopes": sorted(SCOPES),
                "instructions": (
                    "Use this bearer token as the Authorization header for A2A "
                    "SendMessage calls against this server. It will not be shown "
                    "again."
                ),
            }
            if result.plaintext_token is not None
            else None
        ),
        "newly_claimed": result.newly_claimed,
    }
    if result.plaintext_token is None:
        response_body["note"] = "session_already_created_and_claimed"
    return 200, response_body


def handle_session_recovery_request(
    capabilities: CapabilityStore,
    *,
    body: object,
    scopes: frozenset[str] = SCOPES,
) -> tuple[int, dict[str, object]]:
    """Transport-independent core of the `POST /sessions/recover` handler.

    Not x402-gated -- see `server.py`'s module docstring: this route
    costs the buyer nothing extra, since they already paid for
    `session_id` once. Authorization is entirely proof of knowledge of
    the plaintext behind the `recovery_secret_hash` commitment the buyer
    supplied at purchase time; see
    `capabilities.CapabilityStore.recover_session_credential`'s
    docstring for why this is the smallest safe mechanism available and
    why an unrelated caller who does not hold that secret can never use
    this route to obtain or rotate the root credential.

    Returns a generic 403 `InvalidRecoverySecret` for every failure mode
    (unknown `session_id`, a session that never opted into recovery, or a
    wrong secret) -- deliberately never distinguishing them in the
    response, so this route cannot be used to enumerate valid session
    ids.
    """
    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}
    session_id = body.get("session_id")
    recovery_secret = body.get("recovery_secret")
    if not isinstance(session_id, str) or not session_id:
        return 400, {"error": "session_id is required"}
    if not isinstance(recovery_secret, str) or not recovery_secret:
        return 400, {"error": "recovery_secret is required"}

    try:
        _token_id, plaintext = capabilities.recover_session_credential(
            session_id, recovery_secret, scopes
        )
    except InvalidRecoverySecret:
        return 403, {"error": "InvalidRecoverySecret"}

    return 200, {
        "session_id": session_id,
        "root_capability": {
            "token": plaintext,
            "scopes": sorted(scopes),
            "instructions": (
                "Use this bearer token as the Authorization header for A2A "
                "SendMessage calls against this server. It supersedes any "
                "previously issued root credential for this session, which "
                "is now revoked. It will not be shown again."
            ),
        },
    }
