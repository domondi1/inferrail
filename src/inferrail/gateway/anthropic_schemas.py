"""The wire format: an Anthropic-compatible `/v1/messages` contract. See
docs/adr/0014-anthropic-messages-passthrough.md for why this is a
separate schema module from `gateway.schemas` (the OpenAI-shaped
`/v1/chat/completions` contract) rather than a variant of it.

`MessagesRequest` forbids unmodeled top-level fields (same
`extra: "forbid"` discipline as `ChatCompletionRequest`) — an
unsupported real Anthropic parameter fails loudly with a clear error,
never silently ignored.

The non-standard top-level `inferrail` field on `MessagesResponse`
carries the same routing/telemetry metadata `ChatCompletionResponse`
does — reused as-is (`gateway.schemas.InferrailMetadata` is already
wire-format-agnostic). Clients that only speak the standard Anthropic
response shape ignore it, same as OpenAI clients do today.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from inferrail.gateway.schemas import InferrailMetadata
from inferrail.providers.anthropic_base import AnthropicMessage


class MessagesRequest(BaseModel):
    model_config = {"extra": "forbid"}

    # Selects an Inferrail *route* (inferrail.yaml: routes.<name>), or —
    # if `default_provider` is set — is forwarded unchanged as a provider
    # model id. Same convention as ChatCompletionRequest.model.
    model: str
    max_tokens: int
    messages: list[AnthropicMessage]
    system: str | list[dict[str, object]] | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop_sequences: list[str] | None = None
    stream: bool = False
    # Passthrough — see providers.anthropic_base for why these stay
    # loosely typed.
    tools: list[dict[str, object]] | None = None
    tool_choice: dict[str, object] | None = None
    # Anthropic's optional caller metadata (e.g. {"user_id": "..."}) — for
    # the provider's own abuse-monitoring, forwarded verbatim, never read
    # by Inferrail itself. Same treatment as ChatCompletionRequest.user.
    metadata: dict[str, object] | None = None


class MessagesUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None


class MessagesResponse(BaseModel):
    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list[dict[str, object]]
    model: str
    stop_reason: str | None = None
    stop_sequence: str | None = None
    usage: MessagesUsage
    inferrail: InferrailMetadata
