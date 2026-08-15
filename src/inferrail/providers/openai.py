"""OpenAI-compatible provider adapter.

Speaks the OpenAI ``/chat/completions`` wire format over HTTP. Because many
providers (Azure OpenAI behind a compatible shim, vLLM, Together, local
llama.cpp servers, etc.) expose the same shape, this single adapter serves
any of them — only ``base_url`` and ``api_key`` differ. A provider with a
genuinely different wire format would get its own adapter implementing the
same :class:`~inferrail.providers.base.Provider` protocol.
"""

from __future__ import annotations

import httpx

from inferrail.errors import (
    AuthenticationError,
    InvalidRequestError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
)
from inferrail.providers.base import NormalizedChatRequest, NormalizedChatResponse


class OpenAIProvider:
    """Provider adapter for OpenAI and OpenAI-compatible HTTP APIs."""

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
        # `client` is injectable so tests can pass an httpx.MockTransport
        # instead of hitting the network, while exercising the exact same
        # request-building and error-normalization code paths. The auth
        # header is set on the client either way, so an injected client
        # doesn't silently end up sending unauthenticated requests.
        self._client = client or httpx.AsyncClient()
        self._client.headers["Authorization"] = f"Bearer {api_key}"

    async def complete(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> NormalizedChatResponse:
        payload: dict[str, object] = {
            "model": request.model,
            "messages": [m.model_dump() for m in request.messages],
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop"] = request.stop

        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
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

    def _parse_response(self, response: httpx.Response) -> NormalizedChatResponse:
        if response.status_code >= 400:
            raise self._error_for_status(response)

        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            usage = data.get("usage") or {}
        except (KeyError, IndexError, ValueError) as exc:
            raise ProviderError(
                f"provider '{self.name}' returned a malformed response: {exc}",
                provider=self.name,
                status_code=response.status_code,
            ) from exc

        return NormalizedChatResponse(
            content=message.get("content") or "",
            finish_reason=choice.get("finish_reason"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            raw_model=data.get("model"),
            provider_request_id=data.get("id"),
        )

    def _error_for_status(self, response: httpx.Response) -> ProviderError:
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
        # 5xx and anything else unrecognized: treat as a transient upstream
        # failure and let the execution engine's retry policy decide.
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
        """Build the telemetry-safe counterpart of `_extract_error_message`.

        Deliberately excludes any upstream free text — the provider's
        `error.message` field and the raw response body both can (and, for
        some providers, routinely do) echo fragments of the submitted
        request back in error text: content-policy rejections quoting the
        flagged input, validation errors quoting a bad field value. Only a
        short, categorical `error.type`/`error.code` is included, if
        present — these are enum-like values by API convention (e.g.
        `"invalid_request_error"`, `"content_policy_violation"`), not free
        text, and are truncated defensively in case a non-conforming
        provider puts something unexpected there.
        """
        try:
            data = response.json()
        except ValueError:
            data = None
        category = None
        if isinstance(data, dict):
            error_obj = data.get("error")
            if isinstance(error_obj, dict):
                category = error_obj.get("type") or error_obj.get("code")
        summary = f"provider '{self.name}' returned HTTP {response.status_code}"
        if isinstance(category, str) and category:
            summary += f" ({category[:64]})"
        return summary

    async def aclose(self) -> None:
        await self._client.aclose()
