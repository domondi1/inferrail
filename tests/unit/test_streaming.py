"""Engine-level streaming semantics: retry boundary, cancellation, and
never-fabricated accounting under interruption.

Exercises `InferenceEngine.prepare_stream`/`_iter_stream` directly (no
FastAPI, no httpx) against a scriptable fake `Provider`, mirroring how
`test_gateway.py`'s `FakeProvider` tests the non-streaming path — see
`gateway/execution.py`'s module docstring for the two-phase design this
verifies.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from _fakes import FakeProvider, StreamScript

from inferrail.config.models import PriceEntry, RouteConfig
from inferrail.errors import InferrailError, ProviderError, ProviderTimeoutError, RateLimitError
from inferrail.gateway.execution import InferenceEngine
from inferrail.gateway.schemas import ChatCompletionRequest
from inferrail.pricing.resolver import PricingResolver
from inferrail.providers.base import ChatMessage
from inferrail.receipts.schema import InferenceReceipt
from inferrail.receipts.sinks import ReceiptSink
from inferrail.routing.router import Router
from inferrail.telemetry.events import InferenceEvent
from inferrail.telemetry.sinks import TelemetrySink


class InMemoryTelemetrySink(TelemetrySink):
    def __init__(self) -> None:
        self.events: list[InferenceEvent] = []

    def emit(self, event: InferenceEvent) -> None:
        self.events.append(event)


class InMemoryReceiptSink(ReceiptSink):
    def __init__(self) -> None:
        self.receipts: list[InferenceReceipt] = []

    def emit(self, receipt: InferenceReceipt) -> None:
        self.receipts.append(receipt)


def _price() -> PriceEntry:
    return PriceEntry(
        input_usd_per_million=Decimal("1.00"),
        output_usd_per_million=Decimal("2.00"),
        source="test-fixture",
        verified_date=date(2020, 1, 1),
    )


def _engine(
    provider: FakeProvider, *, max_retries: int = 0
) -> tuple[InferenceEngine, InMemoryTelemetrySink, InMemoryReceiptSink]:
    route = RouteConfig(provider="openai", model="gpt-4o-mini", max_retries=max_retries)
    router = Router({"default": route})
    pricing = PricingResolver({}, overrides={"openai": {"gpt-4o-mini": _price()}})
    telemetry = InMemoryTelemetrySink()
    receipts = InMemoryReceiptSink()
    engine = InferenceEngine(router, {"openai": provider}, telemetry, pricing, receipts)
    return engine, telemetry, receipts


def _request(**overrides: object) -> ChatCompletionRequest:
    body: dict[str, object] = {
        "model": "default",
        "messages": [ChatMessage(role="user", content="hello")],
    }
    body.update(overrides)
    return ChatCompletionRequest.model_validate(body)


async def _drain(stream: object) -> list[bytes]:
    return [chunk async for chunk in stream]  # type: ignore[union-attr]


async def test_stream_forwards_chunks_and_records_measured_usage() -> None:
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(
                chunks=[
                    b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n',
                    b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":7,'
                    b'"completion_tokens":4}}\n\n',
                    b"data: [DONE]\n\n",
                ]
            )
        ]
    )
    engine, telemetry, receipts = _engine(provider)

    stream = await engine.prepare_stream(_request(stream=True))
    chunks = await _drain(stream)

    assert chunks == [
        b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n',
        b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":7,'
        b'"completion_tokens":4}}\n\n',
        b"data: [DONE]\n\n",
    ]
    assert len(telemetry.events) == 1
    assert telemetry.events[0].status == "success"
    assert telemetry.events[0].prompt_tokens == 7
    assert telemetry.events[0].completion_tokens == 4
    assert len(receipts.receipts) == 1
    assert receipts.receipts[0].status == "success"
    assert receipts.receipts[0].estimated_cost_usd is not None


async def test_stream_retries_only_before_first_chunk() -> None:
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(chunks=[], error=RateLimitError("slow down", provider="openai")),
            StreamScript(chunks=[b"data: [DONE]\n\n"]),
        ]
    )
    engine, telemetry, _ = _engine(provider, max_retries=1)

    stream = await engine.prepare_stream(_request(stream=True))
    chunks = await _drain(stream)

    assert chunks == [b"data: [DONE]\n\n"]
    assert len(provider.stream_calls) == 2
    assert telemetry.events[0].status == "success"
    assert telemetry.events[0].retry_count == 1


async def test_stream_exhausted_pre_first_chunk_retries_raises_and_emits_error() -> None:
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(chunks=[], error=RateLimitError("slow down", provider="openai")),
            StreamScript(chunks=[], error=RateLimitError("slow down", provider="openai")),
        ]
    )
    engine, telemetry, receipts = _engine(provider, max_retries=1)

    with pytest.raises(RateLimitError):
        await engine.prepare_stream(_request(stream=True))

    assert len(provider.stream_calls) == 2
    assert telemetry.events[0].status == "error"
    assert receipts.receipts[0].status == "error"
    assert receipts.receipts[0].estimated_cost_usd is None


async def test_stream_does_not_retry_after_first_chunk_even_if_error_is_retryable() -> None:
    # RateLimitError.retryable is True — but the failure happens only on
    # the *second* __anext__ call, i.e. after one chunk was already
    # yielded downstream. Retrying here would silently replay
    # already-observed output, which the engine must never do.
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(
                chunks=[b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'],
                error=RateLimitError("upstream reset mid-stream", provider="openai"),
            )
        ]
    )
    engine, telemetry, receipts = _engine(provider, max_retries=3)

    stream = await engine.prepare_stream(_request(stream=True))
    chunks = [chunk async for chunk in stream]

    assert chunks == [b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n']
    assert len(provider.stream_calls) == 1  # never retried
    assert telemetry.events[0].status == "partial"
    assert receipts.receipts[0].status == "partial"


async def test_stream_partial_never_fabricates_cost() -> None:
    # Failure happens before the final usage chunk ever arrives.
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(
                chunks=[b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'],
                error=ProviderTimeoutError("timed out", provider="openai"),
            )
        ]
    )
    engine, _, receipts = _engine(provider)

    stream = await engine.prepare_stream(_request(stream=True))
    async for _ in stream:
        pass

    receipt = receipts.receipts[0]
    assert receipt.status == "partial"
    assert receipt.prompt_tokens is None
    assert receipt.completion_tokens is None
    assert receipt.pricing is None
    assert receipt.estimated_cost_usd is None


async def test_stream_cancellation_emits_partial_and_closes_upstream() -> None:
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(
                chunks=[
                    b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n',
                    b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n',
                    b"data: [DONE]\n\n",
                ]
            )
        ]
    )
    engine, telemetry, receipts = _engine(provider)

    stream = await engine.prepare_stream(_request(stream=True))
    first = await stream.__anext__()
    assert first == b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'

    await stream.aclose()  # simulates Starlette closing the generator on client disconnect

    assert provider.stream_closed_count == 1  # upstream was actually torn down
    assert telemetry.events[0].status == "partial"
    assert telemetry.events[0].error_category == "cancelled"
    assert receipts.receipts[0].status == "partial"


async def test_closing_a_never_iterated_stream_is_a_safe_noop() -> None:
    # A generator function's body never runs until it's driven at least
    # once — closing one that was never iterated is a standard Python
    # no-op that never enters the function body at all, so no telemetry
    # can be emitted and the (already-open) upstream connection isn't
    # explicitly closed by this call. This can only happen in the real
    # ASGI path in a narrow race (Starlette's disconnect watcher wins
    # before it ever pulls the first chunk from an already-connected
    # stream) — a known, documented limitation, not a silent gap: the
    # common, directly-testable case (disconnect *after* streaming has
    # begun) is covered above and does close the upstream connection
    # immediately. This test exists to pin down the no-op behavior itself
    # rather than assert something Python's generator semantics can't
    # actually deliver.
    provider = FakeProvider(
        stream_outcomes=[StreamScript(chunks=[b"data: [DONE]\n\n"])],
    )
    engine, telemetry, _ = _engine(provider)

    stream = await engine.prepare_stream(_request(stream=True))
    await stream.aclose()  # must not raise

    assert provider.stream_closed_count == 0
    assert telemetry.events == []


async def test_stream_n_not_one_rejected_without_calling_provider() -> None:
    engine, telemetry, _ = _engine(FakeProvider())

    with pytest.raises(InferrailError):
        await engine.prepare_stream(_request(stream=True, n=2))

    assert telemetry.events[0].status == "error"
    assert telemetry.events[0].error_category == "unsupported_feature"


async def test_stream_forwards_interleaved_parallel_tool_call_deltas_unmodified() -> None:
    # Two tool calls (index 0 and 1) with fragments interleaved across
    # chunks — exactly how a real provider streams parallel calls.
    # Inferrail must forward every chunk byte-for-byte; reconstructing two
    # distinct calls from them is then purely a client-side concern.
    chunks = [
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a",'
        b'"type":"function","function":{"name":"get_weather","arguments":""}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_b",'
        b'"type":"function","function":{"name":"get_time","arguments":""}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        b'"function":{"arguments":"{\\"city\\": \\"Austin\\"}"}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":1,'
        b'"function":{"arguments":"{\\"tz\\": \\"CST\\"}"}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],'
        b'"usage":{"prompt_tokens":15,"completion_tokens":12}}\n\n',
        b"data: [DONE]\n\n",
    ]
    provider = FakeProvider(stream_outcomes=[StreamScript(chunks=chunks)])
    engine, telemetry, _ = _engine(provider)

    stream = await engine.prepare_stream(_request(stream=True))
    received = await _drain(stream)

    assert received == chunks  # byte-exact passthrough, chunk for chunk
    assert telemetry.events[0].status == "success"
    assert telemetry.events[0].prompt_tokens == 15
    assert telemetry.events[0].completion_tokens == 12

    # Prove a real client actually can reconstruct both calls independently
    # from the forwarded bytes — i.e. the interleaving is genuinely
    # lossless, not just superficially "the same bytes."
    calls: dict[int, dict[str, str]] = {}
    for raw in received:
        for line in raw.split(b"\n\n"):
            if not line.startswith(b"data: ") or line == b"data: [DONE]":
                continue
            event = json.loads(line[len(b"data: ") :])
            for delta_call in event["choices"][0]["delta"].get("tool_calls", []):
                index = delta_call["index"]
                entry = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                entry["id"] = delta_call.get("id", entry["id"])
                function = delta_call.get("function", {})
                entry["name"] = function.get("name", entry["name"])
                entry["arguments"] += function.get("arguments", "")

    assert calls[0] == {"id": "call_a", "name": "get_weather", "arguments": '{"city": "Austin"}'}
    assert calls[1] == {"id": "call_b", "name": "get_time", "arguments": '{"tz": "CST"}'}


async def test_stream_provider_error_before_status_ok_is_retryable_like_non_streaming() -> None:
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(chunks=[], error=ProviderError("5xx", provider="openai", retryable=True)),
            StreamScript(chunks=[b"data: [DONE]\n\n"]),
        ]
    )
    engine, telemetry, _ = _engine(provider, max_retries=1)

    stream = await engine.prepare_stream(_request(stream=True))
    await _drain(stream)

    assert len(provider.stream_calls) == 2
    assert telemetry.events[0].status == "success"
