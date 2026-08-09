"""The provider boundary: the interface every inference backend implements.

This is intentionally the smallest interface that supports one real
implementation (:class:`inferrail.providers.openai.OpenAIProvider`) plus the
obvious near-term need (another OpenAI-compatible endpoint with a different
base URL/key). A provider that isn't OpenAI-shaped at the wire level would
still implement this same ``complete`` contract — it would just do more
translation work inside its own adapter.

v0.1 deliberately supports only single string ``content`` per message (no
multi-part/multimodal content, no tool calls, no streaming). Extending
``ChatMessage``/``NormalizedChatRequest`` is the place to add those later.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    role: Role
    content: str


class NormalizedChatRequest(BaseModel):
    """A chat request, already routed: ``model`` is the provider's model id."""

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] | None = None


class NormalizedChatResponse(BaseModel):
    """A provider's response, stripped of provider-specific wire shape."""

    content: str
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    raw_model: str | None = None
    provider_request_id: str | None = None


class Provider(Protocol):
    """Something that can execute a normalized chat request.

    Implementations must raise :class:`inferrail.errors.ProviderError` (or a
    subclass) on failure rather than letting transport-level exceptions
    escape, so the execution engine's retry/telemetry logic never needs to
    know about a specific provider's client library.
    """

    name: str

    async def complete(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> NormalizedChatResponse: ...
