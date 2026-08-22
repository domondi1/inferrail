"""OpenAI-compatible provider adapter.

Speaks the OpenAI ``/chat/completions`` wire format over HTTP. Because many
providers (Azure OpenAI behind a compatible shim, vLLM, Together, local
llama.cpp servers, etc.) expose the same shape, this single adapter serves
any of them — only ``base_url`` and ``api_key`` differ. A provider with a
genuinely different wire format would get its own adapter implementing the
same :class:`~inferrail.providers.base.Provider` protocol.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import httpx

from inferrail.errors import (
    AuthenticationError,
    InvalidRequestError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
)
from inferrail.providers.base import (
    FunctionCall,
    NormalizedChatRequest,
    NormalizedChatResponse,
    ToolCall,
)


class OpenAIProvider:
    """Provider adapter for OpenAI and OpenAI-compatible HTTP APIs."""

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        is_verified_openai: bool = False,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # Mirrors the pricing catalog's own gate (ADR-0005): only a
        # provider verifiably running OpenAI's own API gets an
        # OpenAI-specific request augmentation auto-applied (here:
        # stream_options.include_usage — see `stream()`). An
        # `openai_compatible` endpoint that merely shares the wire shape
        # is never assumed to support an extension it never advertised.
        self._is_verified_openai = is_verified_openai
        # `client` is injectable so tests can pass an httpx.MockTransport
        # instead of hitting the network, while exercising the exact same
        # request-building and error-normalization code paths. The auth
        # header is set on the client either way, so an injected client
        # doesn't silently end up sending unauthenticated requests.
        self._client = client or httpx.AsyncClient()
        self._client.headers["Authorization"] = f"Bearer {api_key}"

    def _require_api_key(self) -> None:
        # `registry.build_providers(require_keys=False)` lets the gateway
        # start with no key configured for this provider (so /health comes
        # up regardless); the deferred check happens here, at the point an
        # actual inference request reaches it — before any network call,
        # so a missing key fails fast and locally rather than as an
        # OpenAI-side 401 round trip.
        if not self._api_key:
            raise AuthenticationError(
                f"provider '{self.name}' has no API key configured: set the "
                "environment variable referenced by this provider's "
                "api_key_env before sending a request through it",
                provider=self.name,
            )

    def _build_payload(self, request: NormalizedChatRequest) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": request.model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop"] = request.stop
        if request.tools is not None:
            payload["tools"] = request.tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
        if request.parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = request.parallel_tool_calls
        if request.stream_options is not None:
            payload["stream_options"] = request.stream_options
        if request.user is not None:
            payload["user"] = request.user
        return payload

    async def complete(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> NormalizedChatResponse:
        self._require_api_key()
        payload = self._build_payload(request)

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

    async def stream(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> AsyncGenerator[bytes, None]:
        """Yield raw upstream SSE bytes, unmodified, chunk by chunk.

        Status/error classification happens before any chunk is yielded
        (so the engine's retry loop can still act on a pre-first-byte
        failure); once the first chunk is yielded, any further failure
        propagates as a plain exception out of the generator — the engine
        treats that as a terminal, non-retryable outcome, never retries
        it, and records it as `partial`.
        """
        self._require_api_key()
        payload = self._build_payload(request)
        payload["stream"] = True
        if self._is_verified_openai and "stream_options" not in payload:
            payload["stream_options"] = {"include_usage": True}

        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
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

    def _parse_response(self, response: httpx.Response) -> NormalizedChatResponse:
        if response.status_code >= 400:
            raise self._error_for_status(response)

        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            usage = data.get("usage") or {}
            # Parsed inside this same try, not after it: a malformed
            # tool_calls entry (missing "id"/"function"/"name"/"arguments")
            # must normalize into a ProviderError like every other
            # malformed-response shape, not escape as a raw, uncaught
            # KeyError — which would bypass the engine's retry/telemetry
            # entirely (it isn't an InferrailError) and silently produce
            # zero InferenceEvent/InferenceReceipt for a failed request.
            tool_calls = self._parse_tool_calls(message.get("tool_calls"))
        except (KeyError, IndexError, ValueError) as exc:
            raise ProviderError(
                f"provider '{self.name}' returned a malformed response: {exc}",
                provider=self.name,
                status_code=response.status_code,
            ) from exc

        return NormalizedChatResponse(
            content=message.get("content"),
            finish_reason=choice.get("finish_reason"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            tool_calls=tool_calls,
            raw_model=data.get("model"),
            provider_request_id=data.get("id"),
        )

    @staticmethod
    def _parse_tool_calls(raw: list[dict[str, Any]] | None) -> list[ToolCall] | None:
        if not raw:
            return None
        tool_calls = []
        for entry in raw:
            function = entry["function"]
            tool_calls.append(
                ToolCall(
                    id=entry["id"],
                    type=entry.get("type", "function"),
                    # `arguments` stays exactly the string the provider
                    # sent — never json.loads'd/re-dumped. See
                    # providers.base's module docstring.
                    function=FunctionCall(
                        name=function["name"], arguments=function["arguments"]
                    ),
                )
            )
        return tool_calls

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
