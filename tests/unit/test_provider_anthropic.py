"""`providers.anthropic.AnthropicProvider` — mirrors test_providers.py's
own structure and rigor for the OpenAI adapter."""

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
from inferrail.providers.anthropic import AnthropicProvider
from inferrail.providers.anthropic_base import AnthropicMessage, AnthropicNormalizedRequest


def _provider(handler: Callable[[httpx.Request], httpx.Response]) -> AnthropicProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return AnthropicProvider(
        name="anthropic", api_key="test-key", base_url="https://example.invalid/v1", client=client
    )


def _request(**overrides: object) -> AnthropicNormalizedRequest:
    defaults: dict[str, object] = dict(
        model="claude-sonnet-5",
        max_tokens=1024,
        messages=[AnthropicMessage(role="user", content="hello")],
    )
    defaults.update(overrides)
    return AnthropicNormalizedRequest(**defaults)  # type: ignore[arg-type]


async def test_complete_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(
            200,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi there"}],
                "model": "claude-sonnet-5",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )

    provider = _provider(handler)

    result = await provider.complete(_request(), timeout=5)

    assert result.content == [{"type": "text", "text": "hi there"}]
    assert result.stop_reason == "end_turn"
    assert result.input_tokens == 5
    assert result.output_tokens == 3
    assert result.raw_id == "msg_123"
    assert result.raw_model == "claude-sonnet-5"


async def test_complete_sends_system_and_max_tokens() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_1", "content": [{"type": "text", "text": "ok"}],
                "model": "claude-sonnet-5", "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    provider = _provider(handler)
    await provider.complete(_request(system="Be terse.", max_tokens=42), timeout=5)

    assert captured["system"] == "Be terse."
    assert captured["max_tokens"] == 42
    assert "stream" not in captured


async def test_complete_authentication_error() -> None:
    provider = _provider(
        lambda r: httpx.Response(
            401, json={"type": "error", "error": {"type": "authentication_error",
                                                   "message": "invalid x-api-key"}}
        )
    )

    with pytest.raises(AuthenticationError, match="invalid x-api-key"):
        await provider.complete(_request(), timeout=5)


async def test_complete_rate_limit_error() -> None:
    provider = _provider(
        lambda r: httpx.Response(
            429, json={"error": {"type": "rate_limit_error", "message": "slow down"}}
        )
    )

    with pytest.raises(RateLimitError) as exc_info:
        await provider.complete(_request(), timeout=5)
    assert exc_info.value.retryable is True


async def test_complete_invalid_request_error() -> None:
    provider = _provider(
        lambda r: httpx.Response(
            400, json={"error": {"type": "invalid_request_error", "message": "bad model"}}
        )
    )

    with pytest.raises(InvalidRequestError):
        await provider.complete(_request(), timeout=5)


async def test_complete_server_error_is_retryable() -> None:
    provider = _provider(lambda r: httpx.Response(500, text="internal error"))

    with pytest.raises(ProviderError) as exc_info:
        await provider.complete(_request(), timeout=5)
    assert exc_info.value.retryable is True


async def test_complete_raises_authentication_error_when_no_api_key_configured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call should be made without an API key")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    provider = AnthropicProvider(
        name="anthropic", api_key="", base_url="https://example.invalid/v1", client=client
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
    canary = "the user asked to IGNORE ALL PREVIOUS INSTRUCTIONS"
    provider = _provider(
        lambda r: httpx.Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "message": f"Invalid request: input '{canary}' was rejected",
                }
            },
        )
    )

    with pytest.raises(InvalidRequestError) as exc_info:
        await provider.complete(_request(), timeout=5)

    assert canary not in exc_info.value.safe_summary
    assert "invalid_request_error" in exc_info.value.safe_summary
    assert canary in str(exc_info.value)


async def test_complete_passes_through_tool_and_content_blocks_unmodified() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}}
                ],
                "model": "claude-sonnet-5", "stop_reason": "tool_use", "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    provider = _provider(handler)
    tools = [
        {
            "name": "get_weather",
            "description": "Get the weather",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]
    tool_result_content = [
        {"type": "tool_result", "tool_use_id": "toolu_0", "content": "sunny"}
    ]
    request = AnthropicNormalizedRequest(
        model="claude-sonnet-5",
        max_tokens=10,
        messages=[AnthropicMessage(role="user", content=tool_result_content)],
        tools=tools,
        tool_choice={"type": "auto"},
    )

    result = await provider.complete(request, timeout=5)

    assert captured["tools"] == tools
    assert captured["tool_choice"] == {"type": "auto"}
    assert captured["messages"][0]["content"] == tool_result_content
    assert result.content == [
        {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}}
    ]


async def test_stream_yields_raw_upstream_bytes() -> None:
    events = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":10}}}\n\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        b'"usage":{"output_tokens":3}}\n\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        return httpx.Response(200, content=events, headers={"content-type": "text/event-stream"})

    provider = _provider(handler)
    chunks = [chunk async for chunk in provider.stream(_request(), timeout=5)]

    assert b"".join(chunks) == events


async def test_stream_error_before_first_chunk_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "no"}})

    provider = _provider(handler)

    with pytest.raises(RateLimitError):
        async for _ in provider.stream(_request(), timeout=5):
            pass
