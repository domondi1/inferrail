"""Anthropic Messages API provider adapter.

Speaks Anthropic's own `/v1/messages` wire format over HTTP against real
`api.anthropic.com` (or an `anthropic_compatible` endpoint that shares
the same shape — only `base_url`/`api_key` differ, same pattern as
`providers.openai.OpenAIProvider` serving both `openai` and
`openai_compatible`). See
docs/adr/0014-anthropic-messages-passthrough.md.

Authentication differs from OpenAI's `Authorization: Bearer` convention:
Anthropic uses an `x-api-key` header plus a required `anthropic-version`
header (a fixed API-version string, not a model version).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx

from inferrail.errors import (
    AuthenticationError,
    InvalidRequestError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
)
from inferrail.providers.anthropic_base import (
    AnthropicNormalizedRequest,
    AnthropicNormalizedResponse,
)

_ANTHROPIC_API_VERSION = "2023-06-01"


class AnthropicProvider:
    """Provider adapter for the real Anthropic API and Anthropic-compatible
    HTTP endpoints."""

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        base_url: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # `client` is injectable so tests can pass an httpx.MockTransport
        # instead of hitting the network — same reasoning as
        # OpenAIProvider.__init__. Headers are set on the client either
        # way, so an injected client doesn't silently end up unauthenticated.
        self._client = client or httpx.AsyncClient()
        self._client.headers["x-api-key"] = api_key
        self._client.headers["anthropic-version"] = _ANTHROPIC_API_VERSION

    def _require_api_key(self) -> None:
        # Same deferred-check reasoning as OpenAIProvider._require_api_key:
        # the gateway must be able to start (and /health come up) before
        # this provider's secret is configured; a missing key only becomes
        # an error when a request actually reaches it.
        if not self._api_key:
            raise AuthenticationError(
                f"provider '{self.name}' has no API key configured: set the "
                "environment variable referenced by this provider's "
                "api_key_env before sending a request through it",
                provider=self.name,
            )

    def _build_payload(self, request: AnthropicNormalizedRequest) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
        }
        if request.system is not None:
            payload["system"] = request.system
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.top_k is not None:
            payload["top_k"] = request.top_k
        if request.stop_sequences is not None:
            payload["stop_sequences"] = request.stop_sequences
        if request.tools is not None:
            payload["tools"] = request.tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
        return payload

    async def complete(
        self, request: AnthropicNormalizedRequest, *, timeout: float
    ) -> AnthropicNormalizedResponse:
        self._require_api_key()
        payload = self._build_payload(request)

        try:
            response = await self._client.post(
                f"{self._base_url}/messages",
                json=payload,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"request to provider '{self.name}' timed out after {timeout}s",
                provider=self.name,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"request to provider '{self.name}' failed: {exc}",
                provider=self.name,
            ) from exc

        return self._parse_response(response)

    async def stream(
        self, request: AnthropicNormalizedRequest, *, timeout: float
    ) -> AsyncGenerator[bytes, None]:
        """Yield raw upstream SSE bytes, unmodified, chunk by chunk. Same
        pre-first-byte-vs-mid-stream failure contract as
        `OpenAIProvider.stream` — see that docstring."""
        self._require_api_key()
        payload = self._build_payload(request)
        payload["stream"] = True

        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/messages",
                json=payload,
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise self._error_for_status(response)
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"request to provider '{self.name}' timed out after {timeout}s",
                provider=self.name,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"request to provider '{self.name}' failed: {exc}",
                provider=self.name,
            ) from exc

    def _parse_response(self, response: httpx.Response) -> AnthropicNormalizedResponse:
        if response.status_code >= 400:
            raise self._error_for_status(response)

        try:
            data = response.json()
            content = data["content"]
            usage = data.get("usage") or {}
        except (KeyError, ValueError) as exc:
            raise ProviderError(
                f"provider '{self.name}' returned a malformed response: {exc}",
                provider=self.name,
                status_code=response.status_code,
            ) from exc

        return AnthropicNormalizedResponse(
            content=content,
            stop_reason=data.get("stop_reason"),
            stop_sequence=data.get("stop_sequence"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            raw_id=data.get("id"),
            raw_model=data.get("model"),
        )

    def _error_for_status(self, response: httpx.Response) -> ProviderError:
        # Anthropic's error body is the same {"error": {"type", "message"}}
        # shape as OpenAI's -- the same extraction logic applies unchanged.
        status = response.status_code
        message = self._extract_error_message(response)
        safe_summary = self._extract_safe_summary(response)
        if status in (401, 403):
            return AuthenticationError(
                message, provider=self.name, status_code=status, safe_summary=safe_summary
            )
        if status == 429:
            return RateLimitError(
                message, provider=self.name, status_code=status, safe_summary=safe_summary
            )
        if status in (400, 404, 422):
            return InvalidRequestError(
                message, provider=self.name, status_code=status, safe_summary=safe_summary
            )
        return ProviderError(
            message,
            provider=self.name,
            status_code=status,
            retryable=status >= 500,
            safe_summary=safe_summary,
        )

    def _extract_error_message(self, response: httpx.Response) -> str:
        try:
            data = response.json()
            detail = data.get("error", {}).get("message") if isinstance(data, dict) else None
        except ValueError:
            detail = None
        detail = detail or response.text[:200]
        return f"provider '{self.name}' returned HTTP {response.status_code}: {detail}"

    def _extract_safe_summary(self, response: httpx.Response) -> str:
        """Same reasoning as `OpenAIProvider._extract_safe_summary`: only a
        short, categorical `error.type` is included, never upstream free
        text, which can echo fragments of the submitted request back."""
        try:
            data = response.json()
        except ValueError:
            data = None
        category = None
        if isinstance(data, dict):
            error_obj = data.get("error")
            if isinstance(error_obj, dict):
                category = error_obj.get("type")
        summary = f"provider '{self.name}' returned HTTP {response.status_code}"
        if isinstance(category, str) and category:
            summary += f" ({category[:64]})"
        return summary

    async def aclose(self) -> None:
        await self._client.aclose()
