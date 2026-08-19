"""The inference execution engine: ties routing, providers, and telemetry
together on the request hot path.

Deliberately separate from `gateway/routes.py` (the HTTP layer) so the full
normalize -> route -> execute -> respond -> emit-telemetry lifecycle is
testable without spinning up FastAPI or making real HTTP calls — see
tests/unit/test_gateway.py.

## Streaming, retries, and cancellation

Non-streaming (`execute`) is unchanged in shape: one atomic
request/response, retried as a whole on a retryable failure, exactly one
`InferenceEvent`/`InferenceReceipt` pair emitted at the end.

Streaming is split into two phases, on purpose:

1. `prepare_stream` — a plain coroutine. Validates the request, resolves
   routing, and opens the upstream connection through to its *first*
   chunk (or a confirmed-successful empty body), retrying only failures
   discovered before that point. This can still raise a normal
   `InferrailError`, caught by `gateway/app.py`'s exception handler like
   any other request — because nothing has been sent to the HTTP client
   yet. This is the retry boundary: once this coroutine returns, no
   further retry is possible, by construction (there is no code path back
   into it).
2. `_iter_stream` — the async generator actually handed to
   `starlette.responses.StreamingResponse`. It forwards every remaining
   upstream byte completely unmodified (never reparsing/reordering tool
   call arguments or any other content), while a side-channel
   `_SseBookkeeper` reads the same bytes *only* to recover the final
   usage block for accounting — never gating or altering what's
   forwarded. Cancellation (client disconnect) arrives here as
   `GeneratorExit`; a provider failure after the first chunk arrives as an
   `InferrailError`. Both are recorded as `status="partial"` — the
   request produced some observable output but never reached a clean,
   fully-measured completion — and neither is retried, since a retry at
   this point would silently replay already-observed agent execution.

Known narrow limitation: if the ASGI server detects a client disconnect
before ever pulling the first item from `_iter_stream` (a race in
Starlette's own disconnect-watcher vs. body-iteration tasks, not something
this module controls), Python's generator semantics make `.aclose()` on a
never-started generator a no-op — the function body never runs, so no
telemetry is emitted and the already-open upstream connection isn't
explicitly closed by that call (it's still cleaned up eventually, via
garbage collection rather than immediately). Once the generator has been
driven at least once — the common case, since `prepare_stream` already
holds an open connection with its first chunk in hand before the ASGI
response even starts — cancellation is immediate and fully accounted for.
See tests/unit/test_streaming.py.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Literal

from inferrail.errors import (
    AuthenticationError,
    InferrailError,
    InvalidRequestError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    RoutingError,
    UnsupportedFeatureError,
)
from inferrail.gateway.schemas import (
    ChatCompletionChoice,
    ChatCompletionChoiceMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionUsage,
    InferrailMetadata,
)
from inferrail.pricing.resolver import PricingResolver
from inferrail.providers.base import NormalizedChatRequest, NormalizedChatResponse, Provider
from inferrail.receipts.builder import build_receipt, new_receipt_id
from inferrail.receipts.sinks import ReceiptSink
from inferrail.routing.router import Router, RoutingContext, RoutingDecision
from inferrail.telemetry.events import ErrorCategory, InferenceEvent
from inferrail.telemetry.sinks import TelemetrySink

_RETRY_BACKOFF_BASE_SECONDS = 0.5
_UNKNOWN = "unknown"

_StreamStatus = Literal["success", "error", "partial"]


def _categorize(exc: InferrailError) -> ErrorCategory:
    if isinstance(exc, AuthenticationError):
        return "authentication"
    if isinstance(exc, RateLimitError):
        return "rate_limit"
    if isinstance(exc, ProviderTimeoutError):
        return "timeout"
    if isinstance(exc, InvalidRequestError):
        return "invalid_request"
    if isinstance(exc, ProviderError):
        return "provider"
    if isinstance(exc, RoutingError):
        return "routing"
    if isinstance(exc, UnsupportedFeatureError):
        return "unsupported_feature"
    return "provider"


class _SseBookkeeper:
    """Reads forwarded SSE bytes *only* to recover the final usage block.

    Never gates, reorders, or mutates anything — `_iter_stream` forwards
    every chunk to the client regardless of what this finds. A chunk that
    doesn't parse as a complete SSE event yet, or as JSON, is simply not
    accounted for yet (or ever, if malformed) — this never raises and
    never affects what's sent downstream.
    """

    def __init__(self) -> None:
        self._buffer = b""
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in self._buffer:
            event, self._buffer = self._buffer.split(b"\n\n", 1)
            self._handle_event(event)

    def _handle_event(self, event: bytes) -> None:
        for line in event.split(b"\n"):
            if not line.startswith(b"data:"):
                continue
            data = line[len(b"data:") :].strip()
            if not data or data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            usage = obj.get("usage") if isinstance(obj, dict) else None
            if isinstance(usage, dict):
                self.prompt_tokens = usage.get("prompt_tokens")
                self.completion_tokens = usage.get("completion_tokens")


@dataclass
class _StreamContext:
    request_id: str
    decision: RoutingDecision
    started: float
    attributes: dict[str, str]
    attempts_used: int
    remaining: AsyncGenerator[bytes, None]
    first_chunk: bytes | None


class InferenceEngine:
    """Executes one chat completion request end to end."""

    def __init__(
        self,
        router: Router,
        providers: dict[str, Provider],
        telemetry: TelemetrySink,
        pricing_resolver: PricingResolver,
        receipts: ReceiptSink,
    ) -> None:
        self._router = router
        self._providers = providers
        self._telemetry = telemetry
        self._pricing_resolver = pricing_resolver
        self._receipts = receipts

    async def execute(
        self, request: ChatCompletionRequest, *, attributes: dict[str, str] | None = None
    ) -> ChatCompletionResponse:
        request_id = f"req_{uuid.uuid4().hex[:20]}"
        started = time.perf_counter()
        attributes = attributes or {}

        self._reject_unsupported(request, request_id, started, attributes)
        decision, provider, normalized_request = await self._resolve(
            request, request_id, started, attributes
        )

        return await self._execute_with_retries(
            request_id, decision, provider, normalized_request, started, attributes
        )

    async def prepare_stream(
        self, request: ChatCompletionRequest, *, attributes: dict[str, str] | None = None
    ) -> AsyncIterator[bytes]:
        """Validate, route, and open the upstream stream through the first
        chunk. See module docstring for why this must complete — including
        all retries — before the caller starts an ASGI streaming response.
        """
        request_id = f"req_{uuid.uuid4().hex[:20]}"
        started = time.perf_counter()
        attributes = attributes or {}

        self._reject_unsupported(request, request_id, started, attributes)
        decision, provider, normalized_request = await self._resolve(
            request, request_id, started, attributes
        )
        ctx = await self._open_stream_with_retries(
            request_id, decision, provider, normalized_request, started, attributes
        )
        return self._iter_stream(ctx)

    def _reject_unsupported(
        self,
        request: ChatCompletionRequest,
        request_id: str,
        started: float,
        attributes: dict[str, str],
    ) -> None:
        if request.n is not None and request.n != 1:
            unsupported_error = UnsupportedFeatureError(
                "n != 1 is not yet supported by Inferrail"
            )
            self._emit_failure(
                request_id, request.model, _UNKNOWN, _UNKNOWN, 0, started,
                unsupported_error, attributes,
            )
            raise unsupported_error

    async def _resolve(
        self,
        request: ChatCompletionRequest,
        request_id: str,
        started: float,
        attributes: dict[str, str],
    ) -> tuple[RoutingDecision, Provider, NormalizedChatRequest]:
        try:
            decision = self._router.resolve(RoutingContext(requested_route=request.model))
        except RoutingError as exc:
            self._emit_failure(
                request_id, request.model, _UNKNOWN, _UNKNOWN, 0, started, exc, attributes
            )
            raise

        provider = self._providers.get(decision.provider_name)
        if provider is None:
            missing_provider_error = RoutingError(
                f"route '{decision.route_name}' references provider "
                f"'{decision.provider_name}', which is not configured"
            )
            self._emit_failure(
                request_id, decision.route_name, decision.provider_name,
                decision.model, 0, started, missing_provider_error, attributes,
            )
            raise missing_provider_error

        normalized_request = NormalizedChatRequest(
            model=decision.model,
            messages=request.messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            top_p=request.top_p,
            stop=request.stop,
            tools=request.tools,
            tool_choice=request.tool_choice,
            parallel_tool_calls=request.parallel_tool_calls,
            stream_options=request.stream_options,
        )
        return decision, provider, normalized_request

    async def _execute_with_retries(
        self,
        request_id: str,
        decision: RoutingDecision,
        provider: Provider,
        normalized_request: NormalizedChatRequest,
        started: float,
        attributes: dict[str, str],
    ) -> ChatCompletionResponse:
        for attempt in range(decision.max_retries + 1):
            try:
                result = await provider.complete(
                    normalized_request, timeout=decision.timeout_seconds
                )
            except InferrailError as exc:
                is_last_attempt = attempt == decision.max_retries
                if not exc.retryable or is_last_attempt:
                    self._emit_failure(
                        request_id, decision.route_name, decision.provider_name,
                        decision.model, attempt, started, exc, attributes,
                    )
                    raise
                await asyncio.sleep(_RETRY_BACKOFF_BASE_SECONDS * (attempt + 1))
                continue

            latency_ms = self._elapsed_ms(started)
            self._telemetry.emit(
                InferenceEvent(
                    request_id=request_id,
                    route=decision.route_name,
                    provider=decision.provider_name,
                    model=decision.model,
                    status="success",
                    total_latency_ms=latency_ms,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    retry_count=attempt,
                )
            )
            self._receipts.emit(
                build_receipt(
                    receipt_id=new_receipt_id(),
                    request_id=request_id,
                    route=decision.route_name,
                    provider=decision.provider_name,
                    model=decision.model,
                    status="success",
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    attributes=attributes,
                    total_latency_ms=latency_ms,
                    retry_count=attempt,
                    pricing_resolver=self._pricing_resolver,
                )
            )
            return self._build_response(request_id, decision, result, latency_ms, attempt)

        # Unreachable: the loop above always either returns or raises.
        raise AssertionError("retry loop exited without returning or raising")

    async def _open_stream_with_retries(
        self,
        request_id: str,
        decision: RoutingDecision,
        provider: Provider,
        normalized_request: NormalizedChatRequest,
        started: float,
        attributes: dict[str, str],
    ) -> _StreamContext:
        for attempt in range(decision.max_retries + 1):
            generator = provider.stream(normalized_request, timeout=decision.timeout_seconds)
            try:
                first_chunk: bytes | None = await generator.__anext__()
            except StopAsyncIteration:
                # Connection and status were fine; the upstream body was
                # simply empty. Treat as a (degenerate) success rather
                # than retrying — retrying here would not be
                # pre-first-byte in spirit, since the connection already
                # succeeded.
                return _StreamContext(
                    request_id, decision, started, attributes, attempt, generator, None
                )
            except InferrailError as exc:
                is_last_attempt = attempt == decision.max_retries
                if not exc.retryable or is_last_attempt:
                    self._emit_failure(
                        request_id, decision.route_name, decision.provider_name,
                        decision.model, attempt, started, exc, attributes,
                    )
                    raise
                await asyncio.sleep(_RETRY_BACKOFF_BASE_SECONDS * (attempt + 1))
                continue

            return _StreamContext(
                request_id, decision, started, attributes, attempt, generator, first_chunk
            )

        raise AssertionError("retry loop exited without returning or raising")

    async def _iter_stream(self, ctx: _StreamContext) -> AsyncIterator[bytes]:
        bookkeeper = _SseBookkeeper()
        chunks_yielded = 0
        status: _StreamStatus = "success"
        error_category: ErrorCategory | None = None
        error_message: str | None = None
        http_status: int | None = None

        try:
            if ctx.first_chunk:
                bookkeeper.feed(ctx.first_chunk)
                chunks_yielded += 1
                yield ctx.first_chunk
            async for chunk in ctx.remaining:
                bookkeeper.feed(chunk)
                chunks_yielded += 1
                yield chunk
        except GeneratorExit:
            status = "partial" if chunks_yielded else "error"
            error_category = "cancelled"
            error_message = "client disconnected before the stream completed"
            raise
        except InferrailError as exc:
            status = "partial" if chunks_yielded else "error"
            error_category = _categorize(exc)
            error_message = exc.safe_summary
            http_status = getattr(exc, "status_code", None)
        finally:
            # `async for` does not close the iterable it loops over when
            # the loop is interrupted (by GeneratorExit or any other
            # exception) — only a clean, fully-consumed loop reaches
            # StopAsyncIteration on its own. Close explicitly on every
            # exit path so an abandoned upstream connection is torn down
            # immediately instead of waiting on garbage collection. Safe
            # to call even when `ctx.remaining` already finished on its
            # own (a no-op in that case).
            await ctx.remaining.aclose()
            self._emit_stream_outcome(
                ctx, bookkeeper, status, error_category, error_message, http_status
            )

    def _emit_stream_outcome(
        self,
        ctx: _StreamContext,
        bookkeeper: _SseBookkeeper,
        status: _StreamStatus,
        error_category: ErrorCategory | None,
        error_message: str | None,
        http_status: int | None,
    ) -> None:
        latency_ms = self._elapsed_ms(ctx.started)
        self._telemetry.emit(
            InferenceEvent(
                request_id=ctx.request_id,
                route=ctx.decision.route_name,
                provider=ctx.decision.provider_name,
                model=ctx.decision.model,
                status=status,
                error_category=error_category,
                error_message=error_message,
                http_status=http_status,
                total_latency_ms=latency_ms,
                prompt_tokens=bookkeeper.prompt_tokens,
                completion_tokens=bookkeeper.completion_tokens,
                retry_count=ctx.attempts_used,
            )
        )
        # A partial/error stream may still have real, measured usage (the
        # provider's final usage chunk can arrive right before a late
        # failure) — build_receipt only prices when both token counts are
        # actually known, so this never fabricates a cost either way.
        self._receipts.emit(
            build_receipt(
                receipt_id=new_receipt_id(),
                request_id=ctx.request_id,
                route=ctx.decision.route_name,
                provider=ctx.decision.provider_name,
                model=ctx.decision.model,
                status=status,
                prompt_tokens=bookkeeper.prompt_tokens,
                completion_tokens=bookkeeper.completion_tokens,
                attributes=ctx.attributes,
                total_latency_ms=latency_ms,
                retry_count=ctx.attempts_used,
                pricing_resolver=self._pricing_resolver,
            )
        )

    def _build_response(
        self,
        request_id: str,
        decision: RoutingDecision,
        result: NormalizedChatResponse,
        latency_ms: float,
        retry_count: int,
    ) -> ChatCompletionResponse:
        total_tokens = None
        if result.prompt_tokens is not None and result.completion_tokens is not None:
            total_tokens = result.prompt_tokens + result.completion_tokens

        return ChatCompletionResponse(
            id=request_id,
            created=int(time.time()),
            model=decision.model,
            choices=[
                ChatCompletionChoice(
                    message=ChatCompletionChoiceMessage(
                        content=result.content,
                        tool_calls=result.tool_calls,
                    ),
                    finish_reason=result.finish_reason,
                )
            ],
            usage=ChatCompletionUsage(
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=total_tokens,
            ),
            inferrail=InferrailMetadata(
                request_id=request_id,
                route=decision.route_name,
                provider=decision.provider_name,
                total_latency_ms=latency_ms,
                retry_count=retry_count,
            ),
        )

    def _emit_failure(
        self,
        request_id: str,
        route: str,
        provider: str,
        model: str,
        retry_count: int,
        started: float,
        exc: InferrailError,
        attributes: dict[str, str],
    ) -> None:
        latency_ms = self._elapsed_ms(started)
        self._telemetry.emit(
            InferenceEvent(
                request_id=request_id,
                route=route,
                provider=provider,
                model=model,
                status="error",
                error_category=_categorize(exc),
                error_message=exc.safe_summary,
                http_status=getattr(exc, "status_code", None),
                total_latency_ms=latency_ms,
                retry_count=retry_count,
            )
        )
        # A failed request never produced usage: cost is None, not 0 — see
        # inferrail.receipts.builder.build_receipt.
        self._receipts.emit(
            build_receipt(
                receipt_id=new_receipt_id(),
                request_id=request_id,
                route=route,
                provider=provider,
                model=model,
                status="error",
                prompt_tokens=None,
                completion_tokens=None,
                attributes=attributes,
                total_latency_ms=latency_ms,
                retry_count=retry_count,
                pricing_resolver=self._pricing_resolver,
            )
        )

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return (time.perf_counter() - started) * 1000
