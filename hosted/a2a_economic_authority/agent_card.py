"""Inferrail Economic Authority — public Agent Card.

Builds the `a2a.types.AgentCard` served at `/.well-known/agent-card.json`.
Declares exactly the six direct operations this service exposes
(reserve/grant/consume/settle/status/revoke) and an HTTP Bearer security
scheme -- callers authenticate with a capability token in the standard
`Authorization: Bearer <token>` header, never inside a message.

No streaming, no push notifications: every operation here completes (or
parks at `TASK_STATE_AUTH_REQUIRED`) within a single request/response, so
there is nothing to stream.

## Discovery gap this module closes

The Agent Card is the *only* thing a genuinely new external agent sees
before it has a capability token -- everything past it requires
authentication it doesn't have yet. Two things follow from that:

1. **Nothing internal-only belongs here.** This project's own phase
   sequencing (Phase A/B/C/...) is meaningful to this repo's own
   engineering history; it is meaningless, and actively confusing, to an
   external caller who has no way to look that vocabulary up. Earlier
   versions of this card leaked it anyway (`version: "0.1.0-phaseB"`,
   `"no payment yet (Phase B)"` in the description) -- both wrong the
   moment Phase C's `POST /sessions` is actually enabled on a given
   deployment, since neither field ever reflected runtime configuration.
2. **A card with no capability token yet must still tell the caller what
   to do next.** `documentation_url` (a real `AgentCard` field, not
   invented here) always points at the public contract doc. When this
   deployment has paid session creation enabled
   (`ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS` set), an `AgentExtension`
   under `AgentCapabilities.extensions` -- the standard A2A mechanism for
   declaring an out-of-band capability, not a fabricated `skills` entry
   -- carries the exact purchase endpoint and price. `POST /sessions` is
   deliberately **never** listed as a `skills` entry: it is a plain HTTP
   route outside the A2A `SendMessage` pipeline (see `server.py`'s module
   docstring), and `access_control.py` locks every other standard A2A
   method down regardless of credential -- declaring it as a `skill`
   would claim an invocation path (`SendMessage`) that does not exist for
   it and that this service actively rejects.
"""

from __future__ import annotations

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol
from google.protobuf.struct_pb2 import Struct

BEARER_SECURITY_SCHEME = "capabilityBearer"

# The public, agent-facing contract doc -- always current on `main`, no
# separately hosted docs site required. Never a relative path: this URL
# is read by external callers, not rendered inside this repo.
DOCUMENTATION_URL = (
    "https://github.com/domondi1/inferrail/blob/main/docs/capabilities/economic-authority.md"
)

# Identifies the session-purchase capability when it's enabled on a given
# deployment. A2A extension URIs are opaque identifiers, not required to
# resolve to anything (the same role an XML namespace URI plays) -- this
# one is namespaced under a domain Inferrail actually controls. No
# central registry exists for this extension (it predates any formal
# cross-spec convention between A2A and x402), so minting one under our
# own namespace, versioned, is the standards-compliant choice available
# today.
SESSION_PURCHASE_EXTENSION_URI = (
    "https://tryinferrail.com/a2a-extensions/economic-authority-session-purchase-v1"
)

_SKILLS = [
    AgentSkill(
        id="reserve",
        name="Reserve authority",
        description=(
            "Bound a new child delegation's spending ceiling out of the caller's "
            "own remaining authority. Requires the 'reserve' scope on the parent "
            "delegation. If the parent's remaining headroom is insufficient, the "
            "task parks at TASK_STATE_AUTH_REQUIRED instead of failing outright; "
            "a subsequent 'grant' on the same task_id can supply the shortfall "
            "and the reservation is retried automatically."
        ),
        tags=["economic-authority", "reserve"],
        input_modes=["application/json"],
        output_modes=["application/json"],
    ),
    AgentSkill(
        id="grant",
        name="Grant additional authority",
        description=(
            "Explicitly increase a delegation's authority envelope. Requires the 'grant' scope."
        ),
        tags=["economic-authority", "grant"],
        input_modes=["application/json"],
        output_modes=["application/json"],
    ),
    AgentSkill(
        id="consume",
        name="Record consumption",
        description=(
            "Record known or unknown consumption against a delegation. Requires "
            "the 'consume' scope. Unknown cost is recorded as an explicit count, "
            "never fabricated into a number."
        ),
        tags=["economic-authority", "consume"],
        input_modes=["application/json"],
        output_modes=["application/json"],
    ),
    AgentSkill(
        id="settle",
        name="Settle a delegation",
        description=(
            "Close a delegation, releasing its unused reservation back to its "
            "parent. Requires the 'settle' scope."
        ),
        tags=["economic-authority", "settle"],
        input_modes=["application/json"],
        output_modes=["application/json"],
    ),
    AgentSkill(
        id="status",
        name="Read status",
        description=(
            "Read a delegation's remaining authority, lineage, and invariant "
            "certainty. Requires the 'read' scope."
        ),
        tags=["economic-authority", "read"],
        input_modes=["application/json"],
        output_modes=["application/json"],
    ),
    AgentSkill(
        id="revoke",
        name="Revoke a delegation tree",
        description=(
            "Revoke a delegation and every descendant in its subtree: settles "
            "each from the leaves up and revokes every capability token scoped "
            "to any of them. Requires the 'revoke' scope on the delegation being "
            "revoked."
        ),
        tags=["economic-authority", "revoke"],
        input_modes=["application/json"],
        output_modes=["application/json"],
    ),
]


def build_agent_card(
    *,
    url: str,
    session_purchase_enabled: bool,
    session_purchase_url: str | None = None,
    session_price_usd: str | None = None,
) -> AgentCard:
    """Builds the public Agent Card for one running server instance.

    `url` is this instance's own base URL (e.g. `http://127.0.0.1:PORT/`) --
    it is not a fixed constant because tests run many independent instances
    on ephemeral ports.

    `session_purchase_enabled` must reflect this instance's *actual*
    runtime configuration (whether `POST /sessions` is wired -- see
    `server.py`'s `build_app`), not a build-time assumption, so the card
    can never drift from what the running process actually does. When
    `True`, `session_purchase_url` and `session_price_usd` are required --
    the card must never advertise a purchase capability without also
    saying, structurally (not just in prose), how to use it.
    """
    if session_purchase_enabled:
        if not session_purchase_url or not session_price_usd:
            raise ValueError(
                "session_purchase_url and session_price_usd are required when "
                "session_purchase_enabled is True"
            )
        description = (
            "The declared authority ceiling is caller-declared and the "
            "ledger is cooperative. Inferrail records and coordinates it "
            "entirely within its own service, does not control any "
            "external wallet, provider, or network spending, and this is "
            "not real-world spend enforcement. Durable, "
            "transport-authenticated delegated spending-ceiling authority "
            "for agent-to-agent work. Direct operations only: no "
            "automatic recursive delegation. A new root capability can be "
            f"purchased via {SESSION_PURCHASE_EXTENSION_URI} (see the "
            f"declared extension below) -- an x402-gated ${session_price_usd} "
            "Base Sepolia test-USDC service fee for creating and hosting the "
            "coordination boundary, never a deposit into, or escrow of, the "
            "buyer-declared authority ceiling itself. See documentation_url "
            "for the full purchase and recovery flow."
        )
        params = Struct()
        params.update(
            {
                "purchase_endpoint": session_purchase_url,
                "price_usd": session_price_usd,
                "network": "eip155:84532",
                "network_name": "Base Sepolia (testnet only)",
            }
        )
        extensions = [
            AgentExtension(
                uri=SESSION_PURCHASE_EXTENSION_URI,
                description=(
                    "Purchase a new Economic Authority root capability via an "
                    "x402-gated POST to the endpoint in params.purchase_endpoint. "
                    "The service fee in params.price_usd pays for creating and "
                    "hosting the coordination boundary -- it is never a deposit "
                    "into, or escrow of, the buyer-declared authority_ceiling_usd. "
                    f"Full flow: {DOCUMENTATION_URL}"
                ),
                required=False,
                params=params,
            )
        ]
    else:
        description = (
            "The declared authority ceiling is caller-declared and the "
            "ledger is cooperative. Inferrail records and coordinates it "
            "entirely within its own service, does not control any "
            "external wallet, provider, or network spending, and this is "
            "not real-world spend enforcement. Durable, "
            "transport-authenticated delegated spending-ceiling authority "
            "for agent-to-agent work. Direct operations only: no "
            "automatic recursive delegation. This deployment does not "
            "currently offer session purchase -- every operation requires an "
            "existing capability token obtained out-of-band. See "
            "documentation_url for the public contract."
        )
        extensions = []

    return AgentCard(
        name="Inferrail Economic Authority",
        description=description,
        version="0.2.0",
        documentation_url=DOCUMENTATION_URL,
        capabilities=AgentCapabilities(
            streaming=False, push_notifications=False, extensions=extensions
        ),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=_SKILLS,
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.JSONRPC,
                protocol_version=PROTOCOL_VERSION_1_0,
            )
        ],
        security_schemes={
            BEARER_SECURITY_SCHEME: SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="bearer",
                    bearer_format="opaque capability token",
                )
            )
        },
        security_requirements=[
            SecurityRequirement(schemes={BEARER_SECURITY_SCHEME: StringList(list=[])})
        ],
    )
