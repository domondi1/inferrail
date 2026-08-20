"""The wire format: an OpenAI-compatible ``/v1/chat/completions`` contract.

Only what Inferrail actually implements is modeled here. Notably still
unsupported: ``n != 1``, multi-part/image content. A request using either
is rejected with a clear 400, not silently ignored. Streaming and tool
calling *are* supported as of Phase 3 — see docs/PRODUCT.md for the full
supported-surface list.

The non-standard top-level ``inferrail`` field carries routing/telemetry
metadata (route, provider, latency, retries). Clients that only speak
standard OpenAI response shapes can ignore it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from inferrail.providers.base import ChatMessage, ToolCall


class ChatCompletionRequest(BaseModel):
    # Selects an Inferrail *route* (inferrail.yaml: routes.<name>) if one by
    # this name is configured — see docs/adr/0002. Otherwise, if
    # `default_provider` is set, forwarded unchanged as a provider model id
    # — see docs/adr/0007.
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] | None = None
    stream: bool = False
    n: int | None = None
    user: str | None = None
    # Passthrough — see providers.base.NormalizedChatRequest for why these
    # stay loosely typed.
    tools: list[dict[str, object]] | None = None
    tool_choice: str | dict[str, object] | None = None
    parallel_tool_calls: bool | None = None
    # Passthrough only — Inferrail auto-injects {"include_usage": true}
    # for a verified OpenAI provider when the caller didn't set this, so a
    # streaming receipt can still be built (see providers/openai.py's
    # `stream()`). A caller's own value always wins.
    stream_options: dict[str, object] | None = None

    @field_validator("stop", mode="before")
    @classmethod
    def _coerce_stop_to_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [value]
        return value


class ChatCompletionChoiceMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    # None when the message only carries tool_calls (finish_reason ==
    # "tool_calls"), matching the upstream OpenAI shape.
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatCompletionChoiceMessage
    finish_reason: str | None = None


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class InferrailMetadata(BaseModel):
    """Inferrail-specific extension data, not part of the OpenAI schema."""

    request_id: str
    route: str
    provider: str
    total_latency_ms: float
    retry_count: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage
    inferrail: InferrailMetadata


class ErrorDetail(BaseModel):
    message: str
    type: str
    # Stable, machine-readable identifier (e.g. "INFERRAIL_E007") — see
    # errors/codes.py. Never None in practice for a v0.1 InferrailError;
    # optional only so a non-InferrailError failure (should not happen,
    # but see gateway/app.py's fallback) doesn't need to fabricate one.
    code: str | None = None
    remediation: str | None = None
    docs_url: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
