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
