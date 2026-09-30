"""Streamed calls under a block budget — see
docs/adr/0021-atomic-budget-reservations.md ("Settlement", "Streaming
usage").

A streamed call reserves before the provider is contacted, exactly like
a non-streamed one. Once the stream ends, the reservation is released if
the provider reported usage and the call could be priced; otherwise it
is held, so a stream without usage can't turn a hard budget into no
budget. A held amount is never presented as the call's cost.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from _budget_harness import (
    ENGINE_HELLO_1000_RESERVATION,
    WORK_ID,
    GatedProvider,
    make_harness,
    settle_tasks,
)
from _fakes import AnthropicFakeProvider, FakeProvider, StreamScript

from inferrail.errors import BudgetExceededError, ProviderError
from inferrail.gateway import execution as execution_module
from inferrail.gateway.anthropic_schemas import MessagesRequest
from inferrail.gateway.schemas import ChatCompletionRequest
from inferrail.providers.base import ChatMessage, NormalizedChatRequest
from inferrail.providers.openai import OpenAIProvider

ATTRS = {"work_id": WORK_ID}

CONTENT = b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
USAGE = b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":4}}\n\n'
DONE = b"data: [DONE]\n\n"


def _chat(**overrides: object) -> ChatCompletionRequest:
    body: dict[str, object] = {
        "model": "default",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 1000,
        "stream": True,
    }
    body.update(overrides)
    return ChatCompletionRequest.model_validate(body)


async def _drain(stream: object) -> list[bytes]:
    return [chunk async for chunk in stream]  # type: ignore[union-attr]


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution_module, "_RETRY_BACKOFF_BASE_SECONDS", 0)


async def test_stream_is_refused_before_the_provider_when_over_budget(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.001")
    provider = FakeProvider(stream_outcomes=[StreamScript(chunks=[CONTENT, USAGE, DONE])])
    engine = h.openai_engine(provider)

    with pytest.raises(BudgetExceededError):
        await engine.prepare_stream(_chat(), attributes=ATTRS)

    assert provider.stream_calls == []


async def test_stream_with_reported_usage_releases_and_is_priced(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("1.00")
    provider = FakeProvider(stream_outcomes=[StreamScript(chunks=[CONTENT, USAGE, DONE])])
    engine = h.openai_engine(provider)

    # While the stream is open, its reservation is outstanding.
    stream = await engine.prepare_stream(_chat(), attributes=ATTRS)
    [active] = h.budgets.list_reservations()
    assert active.state == "active"

    assert await _drain(stream) == [CONTENT, USAGE, DONE]  # bytes unmodified

    assert h.budgets.list_reservations() == []
    [receipt] = h.receipts.read_all()[0]
    assert receipt.estimated_cost_usd == Decimal("0.000015")  # 7 * $1/M + 4 * $2/M
    assert "budget_held_usd" not in receipt.attributes


async def test_stream_without_usage_holds_and_keeps_limiting(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.003")  # room for exactly one reservation
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(chunks=[CONTENT, DONE]),
            StreamScript(chunks=[CONTENT, DONE]),
        ]
    )
    engine = h.openai_engine(provider)

    await _drain(await engine.prepare_stream(_chat(), attributes=ATTRS))

    [held] = h.budgets.list_reservations()
    assert held.state == "held"
    [receipt] = h.receipts.read_all()[0]
    assert receipt.estimated_cost_usd is None  # still honest: unknown
    assert Decimal(receipt.attributes["budget_held_usd"]) == ENGINE_HELLO_1000_RESERVATION
    # Without the hold this stream would count as $0 and the budget would
    # never stop anything.
    with pytest.raises(BudgetExceededError):
        await engine.prepare_stream(_chat(), attributes=ATTRS)
    assert len(provider.stream_calls) == 1


async def test_cancelled_stream_without_usage_holds(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("1.00")
    provider = FakeProvider(stream_outcomes=[StreamScript(chunks=[CONTENT, CONTENT, DONE])])
    engine = h.openai_engine(provider)

    stream = await engine.prepare_stream(_chat(), attributes=ATTRS)
    await stream.__anext__()
    await stream.aclose()  # client disconnect

    [held] = h.budgets.list_reservations()
    assert held.state == "held"
    [receipt] = h.receipts.read_all()[0]
    assert receipt.status == "partial"
    assert "budget_held_usd" in receipt.attributes


async def test_stream_failing_after_usage_arrived_releases(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("1.00")
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(
                chunks=[CONTENT, USAGE],
                error=ProviderError("dropped", provider="openai"),
            )
        ]
    )
    engine = h.openai_engine(provider)

    await _drain(await engine.prepare_stream(_chat(), attributes=ATTRS))

    assert h.budgets.list_reservations() == []
    [receipt] = h.receipts.read_all()[0]
    assert receipt.status == "partial"
    assert receipt.estimated_cost_usd is not None


async def test_stream_failing_mid_way_without_usage_holds(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("1.00")
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(chunks=[CONTENT], error=ProviderError("dropped", provider="openai"))
        ]
    )
    engine = h.openai_engine(provider)

    await _drain(await engine.prepare_stream(_chat(), attributes=ATTRS))

    [held] = h.budgets.list_reservations()
    assert held.state == "held"


async def test_stream_http_error_before_first_chunk_releases_and_retries(
    tmp_path: Path,
) -> None:
    h = make_harness(tmp_path)
    h.add_budget("1.00")
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(
                error=ProviderError("boom", provider="openai", status_code=503, retryable=True)
            ),
            StreamScript(chunks=[CONTENT, USAGE, DONE]),
        ]
    )
    engine = h.openai_engine(provider, max_retries=1)

    await _drain(await engine.prepare_stream(_chat(), attributes=ATTRS))

    assert len(provider.stream_calls) == 2
    assert h.budgets.list_reservations() == []


async def test_concurrent_streams_are_admitted_atomically(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.005")  # room for two
    provider = GatedProvider()
    engine = h.openai_engine(provider)

    async def run() -> list[bytes]:
        return await _drain(await engine.prepare_stream(_chat(), attributes=ATTRS))

    tasks = [asyncio.create_task(run()) for _ in range(4)]
    await settle_tasks()
    provider.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert sum(isinstance(r, BudgetExceededError) for r in results) == 2
    assert len(provider.calls) == 2
    assert h.budgets.list_reservations() == []


async def test_anthropic_stream_with_usage_releases(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("1.00")
    provider = AnthropicFakeProvider(
        stream_outcomes=[
            StreamScript(
                chunks=[
                    b'event: message_start\ndata: {"type":"message_start","message":'
                    b'{"usage":{"input_tokens":7,"output_tokens":1}}}\n\n',
                    b'event: message_delta\ndata: {"type":"message_delta","usage":'
                    b'{"output_tokens":4}}\n\n',
                    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
                ]
            )
        ]
    )
    engine = h.anthropic_engine(provider)
    request = MessagesRequest.model_validate(
        {
            "model": "claude",
            "max_tokens": 1000,
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        }
    )

    await _drain(await engine.prepare_stream(request, attributes=ATTRS))

    assert h.budgets.list_reservations() == []
    [receipt] = h.receipts.read_all()[0]
    assert receipt.estimated_cost_usd is not None


# ---------------------------------------------------------------------------
# Requesting usage upstream (verified OpenAI only)
# ---------------------------------------------------------------------------


def _capturing_provider(seen: list[dict[str, object]]) -> OpenAIProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=CONTENT + USAGE + DONE)

    return OpenAIProvider(
        name="openai",
        api_key="k",
        base_url="https://api.openai.com/v1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        is_verified_openai=True,
    )


def _normalized(stream_options: dict[str, object] | None) -> NormalizedChatRequest:
    return NormalizedChatRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="hello")],
        stream_options=stream_options,
    )


async def test_usage_is_requested_when_caller_set_other_stream_options() -> None:
    seen: list[dict[str, object]] = []
    provider = _capturing_provider(seen)

    [_ async for _ in provider.stream(_normalized({"include_obfuscation": False}), timeout=5)]

    assert seen[0]["stream_options"] == {"include_obfuscation": False, "include_usage": True}


async def test_callers_explicit_include_usage_false_is_respected() -> None:
    seen: list[dict[str, object]] = []
    provider = _capturing_provider(seen)

    [_ async for _ in provider.stream(_normalized({"include_usage": False}), timeout=5)]

    assert seen[0]["stream_options"] == {"include_usage": False}
