"""Inferrail Economic Authority — public Agent Card (Phase B).

Builds the `a2a.types.AgentCard` served at `/.well-known/agent-card.json`.
Declares exactly the six direct operations this service exposes
(reserve/grant/consume/settle/status/revoke) and an HTTP Bearer security
scheme -- callers authenticate with a capability token in the standard
`Authorization: Bearer <token>` header, never inside a message.

No streaming, no push notifications: every operation here completes (or
parks at `TASK_STATE_AUTH_REQUIRED`) within a single request/response, so
there is nothing to stream.
"""

from __future__ import annotations

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol

BEARER_SECURITY_SCHEME = "capabilityBearer"

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


def build_agent_card(*, url: str) -> AgentCard:
    """Builds the public Agent Card for one running server instance.

    `url` is this instance's own base URL (e.g. `http://127.0.0.1:PORT/`) --
    it is not a fixed constant because tests run many independent instances
    on ephemeral ports.
    """
    return AgentCard(
        name="Inferrail Economic Authority",
        description=(
            "Durable, transport-authenticated delegated spending-ceiling "
            "authority for agent-to-agent work. Direct operations only: no "
            "automatic recursive delegation, no payment yet (Phase B)."
        ),
        version="0.1.0-phaseB",
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
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
