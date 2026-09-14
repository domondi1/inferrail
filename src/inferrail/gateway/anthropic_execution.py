"""The Anthropic Messages execution engine — parallel to
`gateway.execution.InferenceEngine`, not built on top of it. See
docs/adr/0014-anthropic-messages-passthrough.md for why.

Retry, cancellation, and receipt/telemetry-emission structure is
deliberately the same shape as `InferenceEngine` (see that module's
docstring for the full streaming/retry/cancellation design — it applies
here unchanged in spirit); only the wire-level details differ:

- Usage is recovered from Anthropic's own SSE event sequence, not
  OpenAI's: `input_tokens` arrives once, on `message_start`;
  `output_tokens` arrives on one or more `message_delta` events as a
  *cumulative* count (the last one observed wins).
- The non-streaming response overrides only `id`/`model` (Inferrail's
  own `request_id`/resolved route target) — the provider's own content
  blocks, `stop_reason`, and `stop_sequence` pass through untouched.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Literal

from inferrail.budgets.enforcement import BudgetEnforcer, approx_char_count
from inferrail.errors import (
    AuthenticationError,
    BudgetExceededError,
    InferrailError,
    InvalidRequestError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    RoutingError,
    UnsupportedFeatureError,
)
from inferrail.gateway.anthropic_schemas import (
    MessagesRequest,
    MessagesResponse,
    MessagesUsage,
)
from inferrail.gateway.schemas import InferrailMetadata
from inferrail.pricing.resolver import PricingResolver
from inferrail.providers.anthropic_base import (
    AnthropicMessagesProvider,
    AnthropicNormalizedRequest,
    AnthropicNormalizedResponse,
)
from inferrail.receipts.builder import build_receipt, new_receipt_id
from inferrail.receipts.sinks import ReceiptSink
from inferrail.routing.router import Router, RoutingContext, RoutingDecision
from inferrail.telemetry.events import ErrorCategory, InferenceEvent
from inferrail.telemetry.sinks import TelemetrySink

_RETRY_BACKOFF_BASE_SECONDS = 0.5
_UNKNOWN = "unknown"

_StreamStatus = Literal["success", "error", "partial"]


def _categorize(exc: InferrailError) -> ErrorCategory:
    if isinstance(exc, BudgetExceededError):
        return "budget_exceeded"
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


class _AnthropicSseBookkeeper:
    """Reads forwarded SSE bytes *only* to recover final usage — never
    gates, reorders, or mutates anything, same contract as
    `gateway.execution._SseBookkeeper`.

    Anthropic's usage is split across two event types: `input_tokens`
    arrives once on `message_start`; `output_tokens` arrives on one or
    more `message_delta` events as a *cumulative* count, so the last
    value observed is the final one — never summed across events.
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
            if not data:
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            event_type = obj.get("type")
            if event_type == "message_start":
                message = obj.get("message")
                usage = message.get("usage") if isinstance(message, dict) else None
                if isinstance(usage, dict) and "input_tokens" in usage:
                    self.prompt_tokens = usage.get("input_tokens")
            elif event_type == "message_delta":
                usage = obj.get("usage")
                if isinstance(usage, dict) and "output_tokens" in usage:
                    self.completion_tokens = usage.get("output_tokens")


@dataclass
class _StreamContext:
    request_id: str
    decision: RoutingDecision
    started: float
    attributes: dict[str, str]
    attempts_used: int
    remaining: AsyncGenerator[bytes, None]
    first_chunk: bytes | None


class AnthropicInferenceEngine:
    """Executes one Messages API request end to end."""

    def __init__(
        self,
        router: Router,
        providers: dict[str, AnthropicMessagesProvider],
        telemetry: TelemetrySink,
        pricing_resolver: PricingResolver,
        receipts: ReceiptSink,
        *,
        budgets: BudgetEnforcer | None = None,
    ) -> None:
        self._router = router
        self._providers = providers
        self._telemetry = telemetry
        self._pricing_resolver = pricing_resolver
        self._receipts = receipts
        self._budgets = budgets

    async def execute(
        self, request: MessagesRequest, *, attributes: dict[str, str] | None = None
    ) -> MessagesResponse:
        request_id = f"req_{uuid.uuid4().hex[:20]}"
        started = time.perf_counter()
        attributes = attributes or {}

        decision, provider, normalized_request = await self._resolve(
            request, request_id, started, attributes
        )
        self._check_budgets(request, decision, request_id, started, attributes)

        return await self._execute_with_retries(
            request_id, decision, provider, normalized_request, started, attributes
        )

    async def prepare_stream(
        self, request: MessagesRequest, *, attributes: dict[str, str] | None = None
    ) -> AsyncIterator[bytes]:
        """See `gateway.execution.InferenceEngine.prepare_stream` — same
        two-phase design and the same reason it must complete (including
        all retries) before the caller starts an ASGI streaming response.
        """
        request_id = f"req_{uuid.uuid4().hex[:20]}"
        started = time.perf_counter()
        attributes = attributes or {}

        decision, provider, normalized_request = await self._resolve(
            request, request_id, started, attributes
        )
        self._check_budgets(request, decision, request_id, started, attributes)
        ctx = await self._open_stream_with_retries(
            request_id, decision, provider, normalized_request, started, attributes
        )
        return self._iter_stream(ctx)

    def _check_budgets(
        self,
        request: MessagesRequest,
        decision: RoutingDecision,
        request_id: str,
        started: float,
        attributes: dict[str, str],
    ) -> None:
        """See `gateway.execution.InferenceEngine._check_budgets` — same
        pre-flight check, same no-op when unwired, same requirement that a
        block is recorded (not silent) via `_emit_failure`. `max_tokens`
        is always present here (Anthropic's Messages API requires it), so
        there is no OpenAI-side fallback-constant case to handle on this
        path."""
        if self._budgets is None:
            return
        prompt_chars = approx_char_count(
            [m.model_dump() for m in request.messages]
        ) + approx_char_count(request.system)
        try:
            self._budgets.check(
                provider=decision.provider_name,
                model=decision.model,
                attributes=attributes,
                prompt_chars=prompt_chars,
                max_completion_tokens=request.max_tokens,
            )
        except BudgetExceededError as exc:
            self._emit_failure(
                request_id, decision.route_name, decision.provider_name,
                decision.model, 0, started, exc, attributes,
            )
            raise

    async def _resolve(
        self,
        request: MessagesRequest,
        request_id: str,
        started: float,
        attributes: dict[str, str],
    ) -> tuple[RoutingDecision, AnthropicMessagesProvider, AnthropicNormalizedRequest]:
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
                f"'{decision.provider_name}', which is not configured as an "
                "Anthropic-shaped provider"
            )
            self._emit_failure(
                request_id, decision.route_name, decision.provider_name,
                decision.model, 0, started, missing_provider_error, attributes,
            )
            raise missing_provider_error

        normalized_request = AnthropicNormalizedRequest(
            model=decision.model,
            max_tokens=request.max_tokens,
            messages=request.messages,
            system=request.system,
            temperature=request.temperature,
            top_p=request.top_p,
            top_k=request.top_k,
            stop_sequences=request.stop_sequences,
            tools=request.tools,
            tool_choice=request.tool_choice,
        )
        return decision, provider, normalized_request

    async def _execute_with_retries(
        self,
        request_id: str,
        decision: RoutingDecision,
        provider: AnthropicMessagesProvider,
        normalized_request: AnthropicNormalizedRequest,
        started: float,
        attributes: dict[str, str],
    ) -> MessagesResponse:
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
                    prompt_tokens=result.input_tokens,
                    completion_tokens=result.output_tokens,
                    retry_count=attempt,
                )
            )
            receipt_attributes = self._augment_overrun(
                attributes, decision.provider_name, decision.model,
                result.input_tokens, result.output_tokens,
            )
            self._receipts.emit(
                build_receipt(
                    receipt_id=new_receipt_id(),
                    request_id=request_id,
                    route=decision.route_name,
                    provider=decision.provider_name,
                    model=decision.model,
                    status="success",
                    prompt_tokens=result.input_tokens,
                    completion_tokens=result.output_tokens,
                    attributes=receipt_attributes,
                    total_latency_ms=latency_ms,
                    retry_count=attempt,
                    pricing_resolver=self._pricing_resolver,
                )
            )
            return self._build_response(request_id, decision, result, latency_ms, attempt)

        raise AssertionError("retry loop exited without returning or raising")

    async def _open_stream_with_retries(
        self,
        request_id: str,
        decision: RoutingDecision,
        provider: AnthropicMessagesProvider,
        normalized_request: AnthropicNormalizedRequest,
        started: float,
        attributes: dict[str, str],
    ) -> _StreamContext:
        for attempt in range(decision.max_retries + 1):
            generator = provider.stream(normalized_request, timeout=decision.timeout_seconds)
            try:
                first_chunk: bytes | None = await generator.__anext__()
            except StopAsyncIteration:
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
        bookkeeper = _AnthropicSseBookkeeper()
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
            await ctx.remaining.aclose()
            self._emit_stream_outcome(
                ctx, bookkeeper, status, error_category, error_message, http_status
            )

    def _emit_stream_outcome(
        self,
        ctx: _StreamContext,
        bookkeeper: _AnthropicSseBookkeeper,
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
        receipt_attributes = self._augment_overrun(
            ctx.attributes, ctx.decision.provider_name, ctx.decision.model,
            bookkeeper.prompt_tokens, bookkeeper.completion_tokens,
        )
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
                attributes=receipt_attributes,
                total_latency_ms=latency_ms,
                retry_count=ctx.attempts_used,
                pricing_resolver=self._pricing_resolver,
            )
        )

    def _build_response(
        self,
        request_id: str,
        decision: RoutingDecision,
        result: AnthropicNormalizedResponse,
        latency_ms: float,
        retry_count: int,
    ) -> MessagesResponse:
        return MessagesResponse(
            id=request_id,
            content=result.content,
            model=decision.model,
            stop_reason=result.stop_reason,
            stop_sequence=result.stop_sequence,
            usage=MessagesUsage(
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            ),
            inferrail=InferrailMetadata(
                request_id=request_id,
                route=decision.route_name,
                provider=decision.provider_name,
                total_latency_ms=latency_ms,
                retry_count=retry_count,
                provider_request_id=result.raw_id,
                raw_model=result.raw_model,
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

    def _augment_overrun(
        self,
        attributes: dict[str, str],
        provider: str,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> dict[str, str]:
        """See `gateway.execution.InferenceEngine._augment_overrun`."""
        if self._budgets is None:
            return attributes
        return self._budgets.augment_overrun(
            attributes,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return (time.perf_counter() - started) * 1000
