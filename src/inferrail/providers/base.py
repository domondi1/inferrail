"""The provider boundary: the interface every inference backend implements.

This is intentionally the smallest interface that supports one real
implementation (:class:`inferrail.providers.openai.OpenAIProvider`) plus the
obvious near-term need (another OpenAI-compatible endpoint with a different
base URL/key). A provider that isn't OpenAI-shaped at the wire level would
still implement this same ``complete``/``stream`` contract — it would just
do more translation work inside its own adapter.

v0.1 deliberately supported only single string ``content`` per message (no
tool calls, no streaming). As of Phase 3, tool calling and real streaming
are supported — see ``ToolCall``/``FunctionCall`` below and
:meth:`Provider.stream`. Multi-part/multimodal content remains unsupported.

Tool-call ``arguments`` are always carried as an opaque ``str`` end to end
— never parsed as JSON and re-serialized anywhere in this module or its
callers. Reparsing could reorder keys or reformat numbers, silently
changing bytes a model or a downstream tool-execution step might depend on
matching exactly. See docs/PRODUCT.md and the Phase 2A design notes.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Literal, Protocol

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class FunctionCall(BaseModel):
    name: str
    # Raw JSON text exactly as the model/provider produced it. Deliberately
    # a str, not a parsed object: see module docstring.
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    role: Role
    # None for an assistant message that only carries tool_calls, and for
    # provider-shaped messages generally where content is nullable.
    content: str | None = None
    # Present on assistant messages that requested tool execution.
    tool_calls: list[ToolCall] | None = None
    # Present on role="tool" messages: which tool_call this result answers.
    tool_call_id: str | None = None


class NormalizedChatRequest(BaseModel):
    """A chat request, already routed: ``model`` is the provider's model id."""

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] | None = None
    # Passthrough, deliberately untyped beyond "a list of JSON objects" /
    # "a string or JSON object": Inferrail must never drop or reorder a
    # tool-definition field it doesn't itself know about.
    tools: list[dict[str, object]] | None = None
    tool_choice: str | dict[str, object] | None = None
    parallel_tool_calls: bool | None = None
    stream_options: dict[str, object] | None = None
    # OpenAI's end-user identifier, forwarded verbatim so the provider's own
    # abuse-monitoring/rate-limiting can key off it. Never used by Inferrail
    # itself for anything.
    user: str | None = None


class NormalizedChatResponse(BaseModel):
    """A provider's response, stripped of provider-specific wire shape."""

    content: str | None
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    tool_calls: list[ToolCall] | None = None
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

    def stream(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> AsyncGenerator[bytes, None]:
        """Stream raw upstream SSE bytes, unmodified, chunk by chunk.

        Implementations must raise :class:`inferrail.errors.ProviderError`
        for any failure discovered before the first chunk is yielded (so
        the engine's retry loop can still act on it), and must simply stop
        iterating (letting the underlying exception propagate past the
        last yielded chunk) for a failure discovered mid-stream — the
        engine treats "failed after at least one chunk" as a terminal,
        non-retryable `partial` outcome, never a silent retry.

        Typed as an ``AsyncGenerator`` rather than the broader
        ``AsyncIterator`` specifically so callers can rely on ``.aclose()``
        being present: the engine explicitly closes this on every exit
        path (success, failure, or client cancellation) so an abandoned
        upstream connection is torn down immediately rather than left for
        garbage collection to eventually clean up.
        """
        ...
