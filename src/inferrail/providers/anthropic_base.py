"""The Anthropic Messages API provider boundary — parallel to
`providers.base` (the OpenAI-shaped boundary), not built on top of it.
See docs/adr/0014-anthropic-messages-passthrough.md for why this is a
separate wire-native contract rather than a translation layer over
`NormalizedChatRequest`.

Mirrors `providers.base`'s own shape: the smallest interface that
supports one real implementation
(:class:`inferrail.providers.anthropic.AnthropicProvider` against real
`api.anthropic.com`) plus the obvious near-term need (an
``anthropic_compatible`` endpoint sharing the same wire format).

Message `content` and `tools`/`tool_choice` are deliberately untyped
passthrough (a plain string, or Anthropic's own content-block array) —
never parsed and re-serialized at the block level. A `tool_use` block's
``input`` is a JSON *object*, not a string (unlike OpenAI's
`FunctionCall.arguments`), so this module never round-trips it through
anything narrower than ``dict[str, object]``/``list[dict[str, object]]``:
reparsing could still reorder keys or reformat numbers a tool-execution
step might depend on matching exactly.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Literal, Protocol

from pydantic import BaseModel, Field

AnthropicRole = Literal["user", "assistant"]


class AnthropicMessage(BaseModel):
    role: AnthropicRole
    # A plain string, or Anthropic's content-block array (text, tool_use,
    # tool_result, image, ... blocks) -- passthrough, see module docstring.
    content: str | list[dict[str, object]]


class AnthropicNormalizedRequest(BaseModel):
    """A Messages API request, already routed: `model` is the provider's
    model id. `max_tokens` is required, matching the real API -- there is
    no Inferrail-side default, since only the caller knows what output
    length its use case needs."""

    model: str
    max_tokens: int = Field(gt=0)
    messages: list[AnthropicMessage] = Field(min_length=1)
    # Anthropic's system prompt is a top-level field, not a message with
    # role="system" -- unlike OpenAI's convention. Passthrough: a plain
    # string, or an array of text blocks (for cache_control metadata).
    system: str | list[dict[str, object]] | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop_sequences: list[str] | None = None
    tools: list[dict[str, object]] | None = None
    tool_choice: dict[str, object] | None = None


class AnthropicNormalizedResponse(BaseModel):
    """A provider's response. Unlike `providers.base.NormalizedChatResponse`,
    `content` is not stripped down to a single string plus a typed
    `tool_calls` list -- it stays the provider's own content-block array,
    passthrough, because Inferrail speaks this exact wire format on both
    sides of a real Anthropic passthrough and there is nothing to
    normalize away (see docs/adr/0014)."""

    content: list[dict[str, object]]
    stop_reason: str | None
    stop_sequence: str | None
    input_tokens: int | None
    output_tokens: int | None
    raw_id: str | None = None
    raw_model: str | None = None


class AnthropicMessagesProvider(Protocol):
    """Something that can execute a normalized Messages API request.

    Implementations must raise :class:`inferrail.errors.ProviderError` (or
    a subclass) on failure rather than letting transport-level exceptions
    escape — same contract as `providers.base.Provider`.
    """

    name: str

    async def complete(
        self, request: AnthropicNormalizedRequest, *, timeout: float
    ) -> AnthropicNormalizedResponse: ...

    def stream(
        self, request: AnthropicNormalizedRequest, *, timeout: float
    ) -> AsyncGenerator[bytes, None]:
        """Stream raw upstream SSE bytes, unmodified, chunk by chunk. Same
        pre-first-byte-vs-mid-stream failure contract as
        `providers.base.Provider.stream` — see that docstring."""
        ...
