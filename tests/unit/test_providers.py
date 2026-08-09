from __future__ import annotations

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
