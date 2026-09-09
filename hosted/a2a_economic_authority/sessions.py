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
SEPARATE, deliberately minimal mechanism: `recover_session`, authorized
by a `recovery_secret_hash` the buyer MUST include on the original
`POST /sessions` request (required, not optional -- see
`create_or_recover_session`'s docstring for why) -- a SHA-256 commitment
over a secret the buyer generates and keeps for themselves.

Recovery is keyed on `payment_nonce`, never `session_id`. This matters:
`session_id` is server-generated (`secrets.token_hex(16)`, minted inside
`record_session_purchase`) and the buyer's only way to learn it is a
response from this service -- so keying recovery on it would make
recovery unreachable in precisely the scenario it exists for (every
response for this payment lost). `payment_nonce`, by contrast, is
generated by the BUYER's own x402 client before it ever signs or sends
the payment (`x402.mechanisms.evm.utils.create_nonce`, called from
`ExactEvmScheme.create_payment_payload` -- see the installed x402
2.22.0 SDK), so the buyer already possesses it independent of any
response, and it is also `session_purchases`'s own PRIMARY KEY. Recovery
never depends on the buyer having successfully received anything from
this service (unlike a server-minted recovery token, which would share
the exact same loss-of-response risk as the credential it exists to
recover) -- `recovery_secret_hash` alone is the buyer's own
pre-generated secret, and `payment_nonce` alone is never sufficient
authorization (it is not secret -- see `get_session_purchase_for
_recovery`'s docstring). See `recover_session` and `capabilities.
CapabilityStore.get_session_purchase_for_recovery`/`rotate_session_
credential` for the full guarantee and for why this is the smallest safe
design available, given Phase C has no pre-existing capability token to
check an identity against the way Phase B's `reserve` does
(`authorizing_token_id`).

## Known residual gap: settlement succeeds, process dies before any
## durable write (failure window 1)

Recovery (above) closes every window in which SOME durable
`session_purchases` row already exists for the payment_nonce -- a crash
between that row being written and the root being created (window 2), or
between the root being created and the credential being minted (window
3), or after the credential was minted but the response was lost (window
4) -- because `recover_session` re-runs the remaining idempotent steps
using state already durably recorded from the ORIGINAL request. It
cannot close the window before that first row exists: if the process
dies after the `"upfront"` flow's real on-chain settlement succeeds but
before this module's `record_session_purchase` call ever executes (an
extremely narrow window -- there is no further network call between them,
only a local SQLite `INSERT`), no durable state of any kind exists yet
for that payment_nonce, and `recover_session` correctly refuses it
(`InvalidRecoverySecret`) rather than fabricating a session for a request
it cannot prove ever reached this module.

This is not closeable using only the installed x402 2.22.0 SDK's public
surface. `x402.http.facilitator_client.HTTPFacilitatorClient` exposes
exactly `verify`/`settle`/`get_supported` -- no reconciliation query
("what happened to payment_nonce X") independent of calling `settle`
again. A retry of the identical signed payload calls `settle` again for
the SAME nonce; since EIP-3009 nonces are single-use on-chain, the
facilitator rejects it -- but nothing in this SDK documents that
rejection's `error_reason` as a reliable, forgeable-proof signal
distinguishing "this nonce was already fully settled" from any other
duplicate-submission rejection. `x402.server.x402ResourceServer` does
expose `on_before_settle`/`on_settle_failure` hooks that run synchronously
around the real `settle()` network call and could, in principle, let this
module write a durable "settlement about to be attempted" placeholder
before the money moves and later treat a specific reused-nonce rejection
as proof of a prior success -- but doing so safely requires trusting
CDP's exact, undocumented-in-this-SDK error-reason semantics for that
distinction. Getting it wrong in the unsafe direction (treating an
ambiguous rejection as proof of prior settlement) would let an UNPAID
request obtain a session -- a strictly worse failure than the current
gap, and one this module's core guarantee ("a failed or unsettled
payment must never obtain a usable session") cannot risk. The other
alternative, direct on-chain reconciliation (querying Base Sepolia RPC
for the specific nonce/transaction independent of the facilitator), is a
materially larger, separate engineering effort (an RPC client, contract
log parsing) with no code in this repository today, and is out of scope
for Phase C's "smallest architecture" mandate.

Practically: this window requires the process to die in the interval
between one already-completed remote HTTP call returning and one local,
synchronous SQLite `INSERT` -- categorically narrower than window 4
(a full network round-trip to the buyer, plus buyer-side processing
time), which is the scenario this repair was chiefly built to close. Its
blast radius is also asymmetric with every other window: the buyer's fee
is lost with no automatic recovery path, but no unpaid party ever
benefits, and no double session or double charge is possible. Closing it
completely requires payment orchestration or on-chain reconciliation
work explicitly out of scope here; until built, it must be resolved
operationally (a human-support/refund path) and is called out as a
blocking prerequisite for any real (mainnet) deployment -- never silently
covered by this docstring alone.
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
    SessionPurchase,
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
    recovery_secret_hash: str,
    payment_identifier: str | None = None,
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

    `recovery_secret_hash` is REQUIRED, not optional: a 64-character
    lowercase hex SHA-256 digest the buyer computed over a secret only
    they hold, recorded so a later `sessions.recover_session` call can
    verify that secret without this service ever seeing or storing its
    plaintext. This is required rather than merely offered because Phase
    C is an agent-first paid product: an automated buyer that omits an
    optional safety field is exactly the buyer most likely to never
    notice a lost response, and the guarantee this module exists to
    provide -- a settled payment always yields exactly one recoverable
    session -- cannot honestly be claimed for a purchase that silently
    opted out of the one mechanism that makes it true. There is no safe
    default to substitute for an omitted secret (only the buyer can
    generate one unknown to us before payment), so failing closed here --
    before x402 settlement is even attempted, see `handle_session_request`
    -- is the only option that keeps the guarantee unconditional rather
    than "recoverable, provided the buyer thought to ask."
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
    if not isinstance(recovery_secret_hash, str) or not _RECOVERY_HASH_PATTERN.match(
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

    `body` must include `recovery_secret_hash`: the hex-encoded SHA-256 of
    a secret the buyer generated and kept for themselves. REQUIRED, not
    optional -- see `create_or_recover_session`'s docstring for why an
    agent-first paid product cannot honestly guarantee recovery for a
    purchase that opted out of the one mechanism that makes it possible.
    Rejected with 400 before any economic state is touched (and, at the
    HTTP layer, before x402 settlement is even attempted -- see
    `server.py`) if missing or malformed, exactly like a missing
    `agent_id`. It is durably recorded (once, at first purchase) so a
    later `POST /sessions/recover` call can prove ownership of this
    session without ever sending us the plaintext secret in a request we
    might fail to answer -- see `sessions.recover_session`.
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
    if not isinstance(recovery_secret_hash, str) or not recovery_secret_hash:
        return 400, {"error": "recovery_secret_hash is required"}
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


def recover_session(
    core: EconomicAuthorityStore,
    capabilities: CapabilityStore,
    *,
    payment_nonce: str,
    recovery_secret: str,
    scopes: frozenset[str] = SCOPES,
) -> tuple[SessionPurchase, str, str]:
    """Recovers a buyer's root credential for an already-settled payment,
    keyed on `payment_nonce` -- information the buyer's own x402 client
    generated and held before ever signing or sending the payment (see
    `capabilities.CapabilityStore.get_session_purchase_for_recovery`'s
    docstring) -- rather than the server-generated `session_id`, which a
    buyer who lost every response for this payment would have no way to
    know. Raises `capabilities.InvalidRecoverySecret` if `payment_nonce`
    is unknown or `recovery_secret` does not match the commitment
    recorded at purchase time; an unrelated caller who does not hold the
    real secret can never reach anything past that check.

    Deliberately re-runs the FULL idempotent creation pipeline, not only
    the credential mint: `core.create_root` is idempotent per
    `delegation_id` (a no-op if the root already exists, safe and cheap
    if a crash previously interrupted the purchase path before this step
    ran -- see `sessions.py`'s module docstring on failure windows 2/3),
    so recovery also transparently completes a purchase that settled but
    was never fully materialized, not only one that fully completed and
    was merely lost in transit (failure window 4). This never creates a
    new session, a new root delegation, changes `authority_ceiling_usd`,
    or requires a second payment -- only `capabilities.
    rotate_session_credential`'s atomic revoke-then-reissue of the root
    credential itself does that (and it is always safe to call again: at
    most one root credential for a session is ever live, by construction
    -- see that function's docstring).
    """
    purchase = capabilities.get_session_purchase_for_recovery(payment_nonce, recovery_secret)
    core.create_root(
        event_id=f"session-root:{payment_nonce}",
        delegation_id=purchase.session_id,
        agent_id=purchase.agent_id,
        envelope_usd=Decimal(purchase.authority_ceiling_usd),
    )
    token_id, plaintext = capabilities.rotate_session_credential(purchase.session_id, scopes)
    return purchase, token_id, plaintext


def handle_session_recovery_request(
    core: EconomicAuthorityStore,
    capabilities: CapabilityStore,
    *,
    body: object,
    scopes: frozenset[str] = SCOPES,
) -> tuple[int, dict[str, object]]:
    """Transport-independent core of the `POST /sessions/recover` handler.

    Not x402-gated -- see `server.py`'s module docstring: this route
    costs the buyer nothing extra, since they already paid for this
    session once. The request identifies the payment by `payment_nonce`
    (never `session_id` -- see `recover_session`'s docstring for exactly
    why: the buyer already possesses `payment_nonce` from before payment,
    even having received zero responses from this service for it, which
    is not true of `session_id`). Authorization is entirely proof of
    knowledge of the plaintext behind the `recovery_secret_hash`
    commitment the buyer supplied at purchase time; see
    `capabilities.CapabilityStore.get_session_purchase_for_recovery`'s
    docstring for why this is the smallest safe mechanism available and
    why an unrelated caller who does not hold that secret can never use
    this route to obtain or rotate the root credential.

    Returns a generic 403 `InvalidRecoverySecret` for every failure mode
    (unknown `payment_nonce`, a purchase that never opted into recovery,
    or a wrong secret) -- deliberately never distinguishing them in the
    response, so this route cannot be used to enumerate valid payment
    nonces.
    """
    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}
    payment_nonce = body.get("payment_nonce")
    recovery_secret = body.get("recovery_secret")
    if not isinstance(payment_nonce, str) or not payment_nonce:
        return 400, {"error": "payment_nonce is required"}
    if not isinstance(recovery_secret, str) or not recovery_secret:
        return 400, {"error": "recovery_secret is required"}

    try:
        purchase, _token_id, plaintext = recover_session(
            core, capabilities, payment_nonce=payment_nonce, recovery_secret=recovery_secret,
            scopes=scopes,
        )
    except InvalidRecoverySecret:
        return 403, {"error": "InvalidRecoverySecret"}

    return 200, {
        "session_id": purchase.session_id,
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
