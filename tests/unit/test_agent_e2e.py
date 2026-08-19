"""Real-agent-shaped end-to-end round trips (Workstream 2A.10).

Drives the actual FastAPI ASGI app in-process via `httpx.ASGITransport`
(no TestClient shortcuts, no mocked engine internals) through a genuine
two-turn tool-calling exchange — the shape any OpenAI-SDK-based agent
framework (LangChain, CrewAI, the raw `openai` package, ...) produces:
assistant requests a tool, caller executes it locally and replies with
`role="tool"`, model produces a final answer. Both the non-streaming and
the real-SSE-streaming variants are exercised, backed by a scripted fake
upstream so no network or API key is required.

This intentionally uses raw httpx against the wire protocol rather than
the `openai` Python package: the package isn't a dependency of this
project (see pyproject.toml — the hot path and its tests stay
dependency-minimal by design), and the wire protocol *is* the actual
compatibility contract. A real-SDK/real-framework check was additionally
run out-of-band in an isolated venv, the same way Phase 1's B2A audit
verified langchain-openai/llama-index/crewai — see the Phase 2 report.
"""

from __future__ import annotations

import json

import httpx
import pytest
from _fakes import FakeProvider, StreamScript

from inferrail.config.models import InferrailConfig
from inferrail.gateway import app as app_module
from inferrail.providers.base import NormalizedChatResponse


async def _client(
    monkeypatch: pytest.MonkeyPatch, config: InferrailConfig, provider: FakeProvider
) -> httpx.AsyncClient:
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    app = app_module.create_app(config)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def test_two_turn_tool_call_round_trip_non_streaming(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    provider = FakeProvider(
        outcomes=[
            NormalizedChatResponse(
                content=None,
                finish_reason="tool_calls",
                prompt_tokens=12,
                completion_tokens=8,
                tool_calls=[
                    {
                        "id": "call_xyz",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Austin"}'},
                    }
                ],
            ),
            NormalizedChatResponse(
                content="It's 90F and sunny in Austin.",
                finish_reason="stop",
                prompt_tokens=20,
                completion_tokens=10,
            ),
        ]
    )
    async with await _client(monkeypatch, base_config, provider) as client:
        turn1 = await client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "What's the weather in Austin?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                            },
                        },
                    }
                ],
            },
        )
        assert turn1.status_code == 200
        body1 = turn1.json()
        assert body1["choices"][0]["finish_reason"] == "tool_calls"
        tool_call = body1["choices"][0]["message"]["tool_calls"][0]
        assert tool_call["function"]["name"] == "get_weather"
        arguments = json.loads(tool_call["function"]["arguments"])
        assert arguments == {"city": "Austin"}

        # The caller (an agent framework) executes the tool locally, then
        # submits the result and continues the same conversation.
        turn2 = await client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [
                    {"role": "user", "content": "What's the weather in Austin?"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [tool_call],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": "90F, sunny",
                    },
                ],
            },
        )
        assert turn2.status_code == 200
        body2 = turn2.json()
        assert body2["choices"][0]["message"]["content"] == "It's 90F and sunny in Austin."
        assert body2["choices"][0]["finish_reason"] == "stop"

    # Both turns actually reached the (fake) provider, and the second
    # turn's request carried the tool result through unmodified.
    assert len(provider.calls) == 2
    assert provider.calls[1].messages[-1].role == "tool"
    assert provider.calls[1].messages[-1].content == "90F, sunny"
    assert provider.calls[1].messages[-1].tool_call_id == "call_xyz"


async def test_two_turn_tool_call_round_trip_streaming(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    # Streamed tool-call deltas, the shape a real client reconstructs from:
    # one chunk carries id/name, subsequent chunks carry argument
    # fragments, all under the same tool_calls[].index.
    turn1_chunks = [
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_xyz",'
        b'"type":"function","function":{"name":"get_weather","arguments":""}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        b'"function":{"arguments":"{\\"city\\": "}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        b'"function":{"arguments":"\\"Austin\\"}"}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],'
        b'"usage":{"prompt_tokens":12,"completion_tokens":8}}\n\n',
        b"data: [DONE]\n\n",
    ]
    turn2_chunks = [
        b'data: {"choices":[{"delta":{"content":"It\'s 90F"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" and sunny."},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":20,"completion_tokens":10}}\n\n',
        b"data: [DONE]\n\n",
    ]
    provider = FakeProvider(
        stream_outcomes=[StreamScript(chunks=turn1_chunks), StreamScript(chunks=turn2_chunks)]
    )

    async with await _client(monkeypatch, base_config, provider) as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "default",
                "stream": True,
                "messages": [{"role": "user", "content": "What's the weather in Austin?"}],
                "tools": [
                    {"type": "function", "function": {"name": "get_weather"}},
                ],
            },
        ) as turn1:
            assert turn1.status_code == 200
            raw1 = b"".join([chunk async for chunk in turn1.aiter_bytes()])

        # Reconstruct the tool call the way a real streaming client would:
        # concatenate argument fragments across chunks sharing index 0.
        call_id = None
        name = None
        arguments = ""
        for line in raw1.split(b"\n\n"):
            if not line.startswith(b"data: ") or line == b"data: [DONE]":
                continue
            event = json.loads(line[len(b"data: ") :])
            for delta_call in event["choices"][0]["delta"].get("tool_calls", []):
                call_id = delta_call.get("id", call_id)
                function = delta_call.get("function", {})
                name = function.get("name", name)
                arguments += function.get("arguments", "")

        assert call_id == "call_xyz"
        assert name == "get_weather"
        assert json.loads(arguments) == {"city": "Austin"}

        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "default",
                "stream": True,
                "messages": [
                    {"role": "user", "content": "What's the weather in Austin?"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": arguments},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": call_id, "content": "90F, sunny"},
                ],
            },
        ) as turn2:
            assert turn2.status_code == 200
            raw2 = b"".join([chunk async for chunk in turn2.aiter_bytes()])

    final_text = ""
    for line in raw2.split(b"\n\n"):
        if not line.startswith(b"data: ") or line == b"data: [DONE]":
            continue
        event = json.loads(line[len(b"data: ") :])
        final_text += event["choices"][0]["delta"].get("content", "")

    assert final_text == "It's 90F and sunny."
    assert len(provider.stream_calls) == 2
    assert provider.stream_calls[1].messages[-1].content == "90F, sunny"
