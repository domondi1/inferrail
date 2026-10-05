"""The wire format: an OpenAI-compatible ``/v1/chat/completions`` contract.

Request fields follow an explicit three-list policy (see
docs/adr/0021-atomic-budget-reservations.md): fields Inferrail
interprets, provider-valid fields it forwards unchanged
(``FORWARDED_FIELDS``), and fields it rejects with a stated reason
(``REJECTED_FIELD_REASONS``). Still unsupported: ``n != 1``, audio/file
content parts. See docs/PRODUCT.md for the full supported-surface list.

``ChatCompletionRequest`` forbids every other top-level field
(``model_config``'s ``extra: "forbid"``) rather than silently dropping
it, the default Pydantic v2 behavior, and ``ChatMessage`` does the same
per message. A client sending an unlisted field gets a clear
``INFERRAIL_E006`` error naming it, never a response that silently
ignored it — this is not blind pass-through. See ``gateway/app.py``'s
``RequestValidationError`` handler, which promotes that failure into
Inferrail's normal error shape.

The non-standard top-level ``inferrail`` field carries routing/telemetry
metadata (route, provider, latency, retries). Clients that only speak
standard OpenAI response shapes can ignore it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from inferrail.providers.base import ChatMessage, ToolCall

# Provider-valid fields forwarded to the upstream unchanged. Inferrail
# reads only `max_completion_tokens` (for the budget reservation
# estimate) and `service_tier` (see `ACCEPTED_SERVICE_TIERS`); the rest
# it never interprets. None is stored.
FORWARDED_FIELDS = frozenset(
    {
        "max_completion_tokens",
        "response_format",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "logit_bias",
        "metadata",
        "store",
        "reasoning_effort",
        "verbosity",
        "prediction",
        "prompt_cache_key",
        "prompt_cache_retention",
        "prompt_cache_options",
        "safety_identifier",
        "service_tier",
        "logprobs",
        "top_logprobs",
        "thinking",
    }
)

# Other tiers are billed at different rates from the pricing catalog's,
# so a receipt's cost (and a budget reservation) would be wrong.
ACCEPTED_SERVICE_TIERS = frozenset({"auto", "default"})

# Real OpenAI fields that are rejected on purpose, with the reason given
# in the INFERRAIL_E006 error.
REJECTED_FIELD_REASONS: dict[str, str] = {
    "audio": "audio output tokens aren't priced",
    "modalities": "audio output tokens aren't priced",
    "web_search_options": "per-call web search fees aren't priced",
    "functions": "deprecated by the provider; use tools",
    "function_call": "deprecated by the provider; use tool_choice",
}


class ChatCompletionRequest(BaseModel):
    # Forbid, not the Pydantic default "ignore": a field this schema
    # doesn't model must fail loudly, never vanish silently while the
    # request still appears to succeed. See module docstring.
    model_config = {"extra": "forbid"}

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
    # Passthrough only — Inferrail adds {"include_usage": true} for a
    # verified OpenAI provider when the caller didn't set include_usage, so
    # a streaming receipt can still be built (see providers/openai.py's
    # `stream()`). A caller's own include_usage always wins.
    stream_options: dict[str, object] | None = None

    # FORWARDED_FIELDS — typed only as loosely as needed to never alter
    # the value that is forwarded.
    max_completion_tokens: int | None = None
    response_format: dict[str, object] | None = None
    seed: int | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    logit_bias: dict[str, object] | None = None
    metadata: dict[str, object] | None = None
    store: bool | None = None
    reasoning_effort: str | None = None
    verbosity: str | None = None
    prediction: dict[str, object] | None = None
    prompt_cache_key: str | None = None
    prompt_cache_retention: str | None = None
    prompt_cache_options: dict[str, object] | None = None
    safety_identifier: str | None = None
    service_tier: str | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    # The `thinking` toggle some OpenAI-compatible providers take (e.g.
    # DeepSeek V4: {"type": "enabled" | "disabled"}). Billed through the
    # usage the provider reports (reasoning counts as completion tokens).
    thinking: dict[str, object] | None = None

    def forwarded_fields(self) -> dict[str, object]:
        """The FORWARDED_FIELDS the client actually set, as sent."""
        return self.model_dump(include=set(FORWARDED_FIELDS), exclude_none=True)

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
    refusal: str | None = None
    tool_calls: list[ToolCall] | None = None


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatCompletionChoiceMessage
    finish_reason: str | None = None
    # The provider's `logprobs` object, passed back exactly as received
    # when the client asked for it — never persisted.
    logprobs: dict[str, object] | None = None


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class InferrailMetadata(BaseModel):
    """Inferrail-specific extension data, not part of the OpenAI schema.

    ``request_id`` (also the top-level response ``id``) and ``model`` (the
    top-level response ``model``) are deliberately Inferrail's own request
    identity, not the upstream provider's — they're what correlates this
    response back to its ``InferenceEvent``/``InferenceReceipt``, which
    exist independent of whatever id/model string a particular provider
    happened to echo back. ``provider_request_id``/``raw_model`` below are
    the provider's own values, carried through additively so they're never
    silently discarded — never conflate the two.
    """

    request_id: str
    route: str
    provider: str
    total_latency_ms: float
    retry_count: int = 0
    # The provider's own response id/model, when it returned one — e.g.
    # OpenAI's "chatcmpl-..." and a dated model snapshot like
    # "gpt-4o-mini-2024-07-18". None for a failed request, or a provider
    # that didn't echo one back.
    provider_request_id: str | None = None
    raw_model: str | None = None


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
    # Structured, error-type-specific fields — e.g. a BudgetExceededError's
    # budget_id/scope/limit_usd/spent_so_far_usd/estimated_request_usd, so
    # a block response is machine-readable beyond just `message` (see
    # MISSION.md's v0.3.0 acceptance criteria). Every value here is a
    # plain string, never request/response content — see
    # gateway/app.py's `_error_details`.
    details: dict[str, str] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
