"""The inference execution engine: ties routing, providers, and telemetry
together on the request hot path.

Deliberately separate from `gateway/routes.py` (the HTTP layer) so the full
normalize -> route -> execute -> respond -> emit-telemetry lifecycle is
testable without spinning up FastAPI or making real HTTP calls — see
tests/unit/test_gateway.py.
"""

from __future__ import annotations

import asyncio
import time
import uuid

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

        if request.stream:
            unsupported_error = UnsupportedFeatureError(
                "stream=true is not yet supported by Inferrail"
            )
            self._emit_failure(
                request_id, request.model, _UNKNOWN, _UNKNOWN, 0, started,
                unsupported_error, attributes,
            )
            raise unsupported_error
        if request.n is not None and request.n != 1:
            unsupported_error = UnsupportedFeatureError(
                "n != 1 is not yet supported by Inferrail"
            )
            self._emit_failure(
                request_id, request.model, _UNKNOWN, _UNKNOWN, 0, started,
                unsupported_error, attributes,
            )
            raise unsupported_error

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
        )

        return await self._execute_with_retries(
            request_id, decision, provider, normalized_request, started, attributes
        )

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
                    message=ChatCompletionChoiceMessage(content=result.content),
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
