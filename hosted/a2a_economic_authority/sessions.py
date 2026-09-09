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

Idempotency and crash-safety: `capabilities.record_session_purchase` and
`capabilities.issue_or_rotate_session_credential` together give session
creation the exact same guarantees Phase B's `reserve` flow already has
for its own credential issuance (see that module's docstrings) --
`payment_nonce` is the sole idempotency key, `session_id` is always
server-generated (never caller-supplied, so it can never be raced or
guessed into pointing at someone else's payment), and at most one root
credential for a session is ever live. `core.create_root` is itself
idempotent per `delegation_id`, so calling it on every retry (whether or
not this is the payment's first successful processing) never creates a
second root or changes an already-recorded ceiling.

Root credential exposure follows the same "exposed at most once, then
never again" discipline the rest of this service uses for capability
tokens: the plaintext is returned to the caller only on the transaction
that actually mints it. A payment nonce that has already been
successfully claimed can never mint or expose a replacement -- there is
no `recover_credential`-style flag here the way Phase B's `reserve` has
one, because Phase C has no pre-existing capability token to check an
identity against the way Phase B does (`authorizing_token_id`); the only
thing distinguishing "the legitimate buyer retrying" from "someone else
who has since observed the same, by-then-public settled payment nonce"
would be a signal this design does not have. Gating recovery on
"has anyone successfully claimed this yet" (state-gated, not
identity-gated) is the smallest conservative choice available: it stays
safe against a spent nonce being replayed by an unrelated third party
once the true buyer has already claimed their credential, at the cost of
being unable to help a buyer whose credential was minted and handed back
successfully but then lost after that (the same accepted limitation
`capabilities.InMemoryCredentialHandoff`'s single-use claim already has
for Phase B).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from capabilities import SCOPES, CapabilityStore, SessionAuthorizationConflict
from core import EconomicAuthorityStore, validate_amount, validate_identifier

MAX_PAYMENT_NONCE_LENGTH = 256


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
) -> SessionCreationResult:
    """Creates (or safely re-observes) exactly one session for a single
    verified payment. Safe to call multiple times, concurrently or
    sequentially, for the same `payment_nonce` -- see this module's
    docstring for the full guarantee. Raises `ValueError` for malformed
    input and `capabilities.SessionAuthorizationConflict` if
    `payment_nonce` is already bound to a different `agent_id`,
    `authority_ceiling_usd`, or `service_fee_usd` (one payment proof can
    never purchase two unrelated sessions).
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

    authority_str = _canonical_decimal_str(authority_ceiling_usd)
    fee_str = _canonical_decimal_str(service_fee_usd)

    purchase = capabilities.record_session_purchase(payment_nonce, agent_id, authority_str, fee_str)

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
    `payment_nonce` from the x402 middleware's verified payment payload,
    call this function, and translate the result into a `JSONResponse`.
    """
    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}

    agent_id = body.get("agent_id")
    raw_ceiling = body.get("authority_ceiling_usd")
    if not isinstance(agent_id, str) or not agent_id:
        return 400, {"error": "agent_id is required"}
    if raw_ceiling is None:
        return 400, {"error": "authority_ceiling_usd is required"}
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
