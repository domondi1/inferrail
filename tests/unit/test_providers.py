from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from inferrail.errors import (
    AuthenticationError,
    InvalidRequestError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
)
from inferrail.providers.base import ChatMessage, NormalizedChatRequest
from inferrail.providers.openai import OpenAIProvider


def _provider(handler: Callable[[httpx.Request], httpx.Response]) -> OpenAIProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return OpenAIProvider(
        name="openai", api_key="test-key", base_url="https://example.invalid/v1", client=client
    )


def _request() -> NormalizedChatRequest:
    return NormalizedChatRequest(
        model="gpt-4o-mini", messages=[ChatMessage(role="user", content="hello")]
    )


async def test_complete_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-123",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "hi there"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
        )

    provider = _provider(handler)

    result = await provider.complete(_request(), timeout=5)

    assert result.content == "hi there"
    assert result.finish_reason == "stop"
    assert result.prompt_tokens == 5
    assert result.completion_tokens == 3
    assert result.provider_request_id == "chatcmpl-123"


async def test_complete_authentication_error() -> None:
    provider = _provider(
        lambda r: httpx.Response(401, json={"error": {"message": "invalid api key"}})
    )

    with pytest.raises(AuthenticationError, match="invalid api key"):
        await provider.complete(_request(), timeout=5)


async def test_complete_rate_limit_error() -> None:
    provider = _provider(lambda r: httpx.Response(429, json={"error": {"message": "slow down"}}))

    with pytest.raises(RateLimitError) as exc_info:
        await provider.complete(_request(), timeout=5)
    assert exc_info.value.retryable is True


async def test_complete_invalid_request_error() -> None:
    provider = _provider(
        lambda r: httpx.Response(400, json={"error": {"message": "bad model"}})
    )

    with pytest.raises(InvalidRequestError):
        await provider.complete(_request(), timeout=5)


async def test_complete_server_error_is_retryable() -> None:
    provider = _provider(lambda r: httpx.Response(500, text="internal error"))

    with pytest.raises(ProviderError) as exc_info:
        await provider.complete(_request(), timeout=5)
    assert exc_info.value.retryable is True


async def test_complete_raises_authentication_error_when_no_api_key_configured() -> None:
    # Constructed with an empty key — the state OpenAIProvider ends up in
    # when registry.build_providers(require_keys=False) tolerates a missing
    # secret at server startup (gateway/app.py). The check must happen here,
    # locally, before any network call — a handler that gets invoked at all
    # fails this test.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call should be made without an API key")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    provider = OpenAIProvider(
        name="openai", api_key="", base_url="https://example.invalid/v1", client=client
    )

    with pytest.raises(AuthenticationError, match="no API key configured"):
        await provider.complete(_request(), timeout=5)


async def test_complete_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    provider = _provider(handler)

    with pytest.raises(ProviderTimeoutError) as exc_info:
        await provider.complete(_request(), timeout=5)
    assert exc_info.value.retryable is True


async def test_complete_malformed_response() -> None:
    provider = _provider(lambda r: httpx.Response(200, json={"unexpected": "shape"}))

    with pytest.raises(ProviderError, match="malformed"):
        await provider.complete(_request(), timeout=5)


async def test_error_safe_summary_excludes_upstream_free_text() -> None:
    # Simulates a provider echoing request content in its own error text —
    # e.g. a content-moderation rejection quoting the flagged input.
    canary = "the user asked to IGNORE ALL PREVIOUS INSTRUCTIONS"
    provider = _provider(
        lambda r: httpx.Response(
            400,
            json={
                "error": {
                    "message": f"Invalid request: input '{canary}' was rejected",
                    "type": "invalid_request_error",
                }
            },
        )
    )

    with pytest.raises(InvalidRequestError) as exc_info:
        await provider.complete(_request(), timeout=5)

    assert canary not in exc_info.value.safe_summary
    assert "invalid_request_error" in exc_info.value.safe_summary
    # The full exception message (used for the HTTP error response to the
    # same caller who sent the content) still carries the detail — only
    # `safe_summary`, which feeds telemetry, is sanitized.
    assert canary in str(exc_info.value)


def test_provider_error_safe_summary_is_fail_closed_when_omitted() -> None:
    # Simulates a future provider adapter (or a future edit to this one)
    # that embeds upstream free text in `message` and simply forgets to
    # pass `safe_summary` at all. The default must never fall back to
    # `message` — it must be derived only from `provider`/`status_code`.
    canary = "the customer's exact prompt content"
    exc = ProviderError(
        f"provider 'anthropic' failed: {canary}",
        provider="anthropic",
        status_code=400,
    )

    assert canary not in exc.safe_summary
    assert exc.safe_summary == "provider 'anthropic' error (HTTP 400)"
    # The full message is untouched — still available for the exception /
    # HTTP response path, which is a separate, intentional concern.
    assert canary in str(exc)


async def test_complete_passes_through_tool_fields_unmodified() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "gpt-4o-mini",
                "choices": [
                    {"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = _provider(handler)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    request = NormalizedChatRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="weather?")],
        tools=tools,
        tool_choice="auto",
        parallel_tool_calls=True,
    )

    await provider.complete(request, timeout=5)

    assert captured["tools"] == tools
    assert captured["tool_choice"] == "auto"
    assert captured["parallel_tool_calls"] is True


async def test_complete_forwards_user_field_to_upstream_payload() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-6",
                "model": "gpt-4o-mini",
                "choices": [
                    {"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = _provider(handler)
    request = NormalizedChatRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="hello")],
        user="end-user-42",
    )

    await provider.complete(request, timeout=5)

    assert captured["user"] == "end-user-42"


async def test_complete_omits_user_field_when_not_set() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-7",
                "model": "gpt-4o-mini",
                "choices": [
                    {"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = _provider(handler)

    await provider.complete(_request(), timeout=5)

    assert "user" not in captured


async def test_complete_parses_tool_calls_with_arguments_kept_as_raw_string() -> None:
    # The arguments string below has unusual formatting (extra spaces,
    # specific key order) that a naive json.loads()+json.dumps() round trip
    # would normalize away. It must survive byte-for-byte.
    raw_arguments = '{"city":   "San Francisco",  "unit": "celsius"}'
    provider = _provider(
        lambda r: httpx.Response(
            200,
            json={
                "id": "chatcmpl-2",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "get_weather", "arguments": raw_arguments},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )

    result = await provider.complete(_request(), timeout=5)

    assert result.content is None
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls is not None
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.id == "call_1"
    assert call.function.name == "get_weather"
    assert call.function.arguments == raw_arguments  # byte-exact, never reparsed


async def test_complete_parses_multiple_parallel_tool_calls_preserving_order() -> None:
    provider = _provider(
        lambda r: httpx.Response(
            200,
            json={
                "id": "chatcmpl-4",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_a",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"city": "Austin"}',
                                    },
                                },
                                {
                                    "id": "call_b",
                                    "type": "function",
                                    "function": {
                                        "name": "get_time",
                                        "arguments": '{"tz": "America/Chicago"}',
                                    },
                                },
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 15, "completion_tokens": 12},
            },
        )
    )

    result = await provider.complete(_request(), timeout=5)

    assert result.tool_calls is not None
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].id == "call_a"
    assert result.tool_calls[0].function.name == "get_weather"
    assert result.tool_calls[0].function.arguments == '{"city": "Austin"}'
    assert result.tool_calls[1].id == "call_b"
    assert result.tool_calls[1].function.name == "get_time"
    assert result.tool_calls[1].function.arguments == '{"tz": "America/Chicago"}'


async def test_complete_malformed_tool_call_raises_provider_error_not_raw_keyerror() -> None:
    # A malformed tool_calls entry (missing "function" here) must normalize
    # into a ProviderError the engine's retry/telemetry machinery
    # recognizes — not escape as an uncaught KeyError, which would bypass
    # InferenceEvent/InferenceReceipt emission entirely for this failure.
    provider = _provider(
        lambda r: httpx.Response(
            200,
            json={
                "id": "chatcmpl-5",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{"id": "call_1", "type": "function"}],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )

    with pytest.raises(ProviderError, match="malformed") as exc_info:
        await provider.complete(_request(), timeout=5)

    # Fail-closed regardless: the default safe_summary never derives from
    # the message string at all (see errors/exceptions.py), so it can't
    # carry anything from the malformed entry either.
    assert exc_info.value.safe_summary == "provider 'openai' error (HTTP 200)"


async def test_complete_no_tool_calls_yields_none() -> None:
    provider = _provider(
        lambda r: httpx.Response(
            200,
            json={
                "id": "chatcmpl-3",
                "model": "gpt-4o-mini",
                "choices": [
                    {"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )

    result = await provider.complete(_request(), timeout=5)

    assert result.tool_calls is None


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


def _streaming_provider(
    handler: Callable[[httpx.Request], httpx.Response], *, is_verified_openai: bool = False
) -> OpenAIProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return OpenAIProvider(
        name="openai",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        client=client,
        is_verified_openai=is_verified_openai,
    )


async def test_stream_forwards_raw_bytes_unmodified() -> None:
    raw_body = (
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    provider = _streaming_provider(
        lambda r: httpx.Response(
            200, content=raw_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = [chunk async for chunk in provider.stream(_request(), timeout=5)]

    assert b"".join(chunks) == raw_body


async def test_stream_auto_injects_include_usage_only_for_verified_openai() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    verified = _streaming_provider(handler, is_verified_openai=True)
    async for _ in verified.stream(_request(), timeout=5):
        pass
    assert captured["stream_options"] == {"include_usage": True}

    captured.clear()
    unverified = _streaming_provider(handler, is_verified_openai=False)
    async for _ in unverified.stream(_request(), timeout=5):
        pass
    assert "stream_options" not in captured


async def test_stream_never_overrides_caller_supplied_stream_options() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    provider = _streaming_provider(handler, is_verified_openai=True)
    request = NormalizedChatRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="hi")],
        stream_options={"include_usage": False},
    )

    async for _ in provider.stream(request, timeout=5):
        pass

    assert captured["stream_options"] == {"include_usage": False}


async def test_stream_pre_first_chunk_error_is_classified_like_non_streaming() -> None:
    provider = _streaming_provider(
        lambda r: httpx.Response(429, json={"error": {"message": "slow down"}})
    )

    with pytest.raises(RateLimitError) as exc_info:
        async for _ in provider.stream(_request(), timeout=5):
            pass
    assert exc_info.value.retryable is True


async def test_stream_pre_first_chunk_error_excludes_upstream_free_text() -> None:
    # Mirrors test_error_safe_summary_excludes_upstream_free_text for the
    # stream() code path specifically — it reuses _error_for_status, but
    # confirm that directly rather than assuming it from the non-streaming
    # test alone.
    sentinel = "INFERRAIL_PHASE2_PRIVACY_SENTINEL_9f8e7d"
    provider = _streaming_provider(
        lambda r: httpx.Response(
            400,
            json={
                "error": {
                    "message": f"Invalid request: input '{sentinel}' was rejected",
                    "type": "invalid_request_error",
                }
            },
        )
    )

    with pytest.raises(InvalidRequestError) as exc_info:
        async for _ in provider.stream(_request(), timeout=5):
            pass

    assert sentinel not in exc_info.value.safe_summary
    assert "invalid_request_error" in exc_info.value.safe_summary
    assert sentinel in str(exc_info.value)  # caller-facing detail, same caller


async def test_stream_requires_api_key_before_any_network_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call should be made without an API key")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    provider = OpenAIProvider(
        name="openai", api_key="", base_url="https://example.invalid/v1", client=client
    )

    with pytest.raises(AuthenticationError, match="no API key configured"):
        async for _ in provider.stream(_request(), timeout=5):
            pass


async def test_error_safe_summary_falls_back_to_status_only() -> None:
    # No structured error.type/code, and a non-JSON body that could itself
    # contain echoed content (e.g. an HTML error page reflecting a bad
    # request parameter) — safe_summary must still exclude the raw body.
    provider = _provider(
        lambda r: httpx.Response(500, text="upstream exploded while processing <script>x</script>")
    )

    with pytest.raises(ProviderError) as exc_info:
        await provider.complete(_request(), timeout=5)

    assert exc_info.value.safe_summary == "provider 'openai' returned HTTP 500"
