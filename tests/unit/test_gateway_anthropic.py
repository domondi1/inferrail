"""`/v1/messages` (Anthropic-compatible passthrough) — mirrors
test_gateway.py's structure and rigor for the OpenAI-shaped route,
covering the categories that matter most for a second wire-native
pipeline: routing, streaming byte fidelity, tool/content-block
round-tripping, retries, telemetry, gateway-token auth, and the same
payload-privacy guarantees ADR-0003 requires everywhere. See
docs/adr/0014-anthropic-messages-passthrough.md.
"""

from __future__ import annotations

from typing import Any

import pytest
from _fakes import AnthropicFakeProvider, StreamScript
from fastapi.testclient import TestClient

from inferrail.config.models import InferrailConfig
from inferrail.errors import AuthenticationError
from inferrail.gateway import app as app_module
from inferrail.providers.anthropic_base import AnthropicNormalizedResponse
from inferrail.telemetry.events import InferenceEvent

__all__ = ["AnthropicFakeProvider", "StreamScript"]


class InMemoryTelemetrySink:
    def __init__(self) -> None:
        self.events: list[InferenceEvent] = []

    def emit(self, event: InferenceEvent) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def _no_gateway_token_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFERRAIL_GATEWAY_TOKEN", raising=False)


@pytest.fixture
def anthropic_config_dict() -> dict[str, Any]:
    return {
        "providers": {
            "anthropic": {"type": "anthropic", "api_key_env": "TEST_ANTHROPIC_API_KEY"},
        },
        "routes": {
            "claude": {"provider": "anthropic", "model": "claude-sonnet-5"},
        },
        "telemetry": {"sink": "none"},
        "receipts": {"sink": "none"},
    }


@pytest.fixture
def anthropic_config(anthropic_config_dict: dict[str, Any]) -> InferrailConfig:
    return InferrailConfig.model_validate(anthropic_config_dict)


def _make_anthropic_client(
    monkeypatch: pytest.MonkeyPatch,
    config: InferrailConfig,
    provider: AnthropicFakeProvider,
    telemetry: InMemoryTelemetrySink | None = None,
) -> TestClient:
    monkeypatch.setattr(
        app_module, "build_anthropic_providers", lambda cfg, **_kw: {"anthropic": provider}
    )
    if telemetry is not None:
        monkeypatch.setattr(app_module, "build_telemetry_sink", lambda cfg: telemetry)
    app = app_module.create_app(config)
    return TestClient(app)


def _messages_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "claude",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


def test_messages_success(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: InferrailConfig
) -> None:
    client = _make_anthropic_client(monkeypatch, anthropic_config, AnthropicFakeProvider())

    response = client.post("/v1/messages", json=_messages_body())

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == [{"type": "text", "text": "ok"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 2}
    assert body["role"] == "assistant"
    assert body["type"] == "message"
    # Inferrail's own identity, not the (absent, in this fake) upstream one.
    assert body["id"] == body["inferrail"]["request_id"]
    assert body["model"] == "claude-sonnet-5"
    assert body["inferrail"]["route"] == "claude"
    assert body["inferrail"]["provider"] == "anthropic"


def test_messages_unknown_route(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: InferrailConfig
) -> None:
    client = _make_anthropic_client(monkeypatch, anthropic_config, AnthropicFakeProvider())

    response = client.post("/v1/messages", json=_messages_body(model="nope"))

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "RoutingError"


def test_messages_passthrough_unmatched_model(
    monkeypatch: pytest.MonkeyPatch, anthropic_config_dict: dict[str, Any]
) -> None:
    config = InferrailConfig.model_validate(
        {**anthropic_config_dict, "default_anthropic_provider": "anthropic"}
    )
    client = _make_anthropic_client(monkeypatch, config, AnthropicFakeProvider())

    response = client.post("/v1/messages", json=_messages_body(model="claude-opus-5"))

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "claude-opus-5"
    assert body["inferrail"]["route"] == "passthrough"


def test_messages_authentication_error_maps_to_401(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: InferrailConfig
) -> None:
    provider = AnthropicFakeProvider(
        outcomes=[AuthenticationError("bad key", provider="anthropic")]
    )
    client = _make_anthropic_client(monkeypatch, anthropic_config, provider)

    response = client.post("/v1/messages", json=_messages_body())

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "AuthenticationError"


def test_messages_rejects_unmodeled_field(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: InferrailConfig
) -> None:
    client = _make_anthropic_client(monkeypatch, anthropic_config, AnthropicFakeProvider())

    response = client.post("/v1/messages", json=_messages_body(response_format={"type": "json"}))

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "UnsupportedFeatureError"
    assert "response_format" in response.json()["error"]["message"]


def test_messages_stream_forwards_raw_bytes_and_no_metadata_injected(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: InferrailConfig
) -> None:
    events = (
        b'event: message_start\ndata: {"type":"message_start",'
        b'"message":{"usage":{"input_tokens":10}}}\n\n'
        b'event: content_block_delta\ndata: {"type":"content_block_delta",'
        b'"index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta",'
        b'"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    provider = AnthropicFakeProvider(stream_outcomes=[StreamScript(chunks=[events])])
    client = _make_anthropic_client(monkeypatch, anthropic_config, provider)

    with client.stream("POST", "/v1/messages", json=_messages_body(stream=True)) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = b"".join(response.iter_bytes())

    assert body == events  # byte-for-byte, no inferrail block injected


def test_messages_tool_use_content_block_round_trip(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: InferrailConfig
) -> None:
    provider = AnthropicFakeProvider(
        outcomes=[
            AnthropicNormalizedResponse(
                content=[
                    {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}}
                ],
                stop_reason="tool_use",
                stop_sequence=None,
                input_tokens=10,
                output_tokens=6,
            )
        ]
    )
    client = _make_anthropic_client(monkeypatch, anthropic_config, provider)
    tools = [
        {
            "name": "get_weather",
            "description": "Get the weather",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]

    response = client.post("/v1/messages", json=_messages_body(tools=tools))

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == [
        {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}}
    ]
    assert body["stop_reason"] == "tool_use"
    # The passthrough tool definition reached the provider unmodified.
    assert provider.calls[0].tools == tools


def test_messages_retries_transient_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, anthropic_config_dict: dict[str, Any]
) -> None:
    from inferrail.errors import ProviderError

    config = InferrailConfig.model_validate(
        {
            **anthropic_config_dict,
            "routes": {"claude": {**anthropic_config_dict["routes"]["claude"], "max_retries": 2}},
        }
    )
    provider = AnthropicFakeProvider(
        outcomes=[
            ProviderError("transient", provider="anthropic", retryable=True),
            AnthropicNormalizedResponse(
                content=[{"type": "text", "text": "ok"}], stop_reason="end_turn",
                stop_sequence=None, input_tokens=1, output_tokens=1,
            ),
        ]
    )
    client = _make_anthropic_client(monkeypatch, config, provider)

    response = client.post("/v1/messages", json=_messages_body())

    assert response.status_code == 200
    assert len(provider.calls) == 2
    assert response.json()["inferrail"]["retry_count"] == 1


def test_telemetry_emitted_on_success(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: InferrailConfig
) -> None:
    telemetry = InMemoryTelemetrySink()
    client = _make_anthropic_client(
        monkeypatch, anthropic_config, AnthropicFakeProvider(), telemetry
    )

    client.post("/v1/messages", json=_messages_body())

    assert len(telemetry.events) == 1
    event = telemetry.events[0]
    assert event.status == "success"
    assert event.provider == "anthropic"
    assert event.prompt_tokens == 3
    assert event.completion_tokens == 2


def test_telemetry_emitted_on_error(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: InferrailConfig
) -> None:
    telemetry = InMemoryTelemetrySink()
    provider = AnthropicFakeProvider(
        outcomes=[AuthenticationError("bad key", provider="anthropic")]
    )
    client = _make_anthropic_client(monkeypatch, anthropic_config, provider, telemetry)

    client.post("/v1/messages", json=_messages_body())

    assert len(telemetry.events) == 1
    assert telemetry.events[0].status == "error"
    assert telemetry.events[0].error_category == "authentication"


def test_messages_no_auth_required_by_default(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: InferrailConfig
) -> None:
    client = _make_anthropic_client(monkeypatch, anthropic_config, AnthropicFakeProvider())

    response = client.post("/v1/messages", json=_messages_body())

    assert response.status_code == 200


def test_messages_rejects_missing_token_when_configured(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: InferrailConfig
) -> None:
    monkeypatch.setenv("INFERRAIL_GATEWAY_TOKEN", "secret-token")
    client = _make_anthropic_client(monkeypatch, anthropic_config, AnthropicFakeProvider())

    response = client.post("/v1/messages", json=_messages_body())

    assert response.status_code == 401


def test_messages_accepts_matching_token_when_configured(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: InferrailConfig
) -> None:
    monkeypatch.setenv("INFERRAIL_GATEWAY_TOKEN", "secret-token")
    client = _make_anthropic_client(monkeypatch, anthropic_config, AnthropicFakeProvider())

    response = client.post(
        "/v1/messages",
        json=_messages_body(),
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200


def test_telemetry_never_persists_prompt_or_tool_content(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: InferrailConfig
) -> None:
    """Same ADR-0003 guarantee the OpenAI path is tested for: nothing
    resembling submitted or returned content ever reaches telemetry,
    whether it's plain text or a tool_use/tool_result content block."""
    telemetry = InMemoryTelemetrySink()
    secret_prompt = "the secret launch codes are ALPHA-BRAVO-9"
    secret_tool_input = "confidential-account-42"
    provider = AnthropicFakeProvider(
        outcomes=[
            AnthropicNormalizedResponse(
                content=[
                    {
                        "type": "tool_use", "id": "toolu_1", "name": "lookup",
                        "input": {"account": secret_tool_input},
                    }
                ],
                stop_reason="tool_use", stop_sequence=None, input_tokens=5, output_tokens=4,
            )
        ]
    )
    client = _make_anthropic_client(monkeypatch, anthropic_config, provider, telemetry)

    client.post(
        "/v1/messages",
        json=_messages_body(messages=[{"role": "user", "content": secret_prompt}]),
    )

    serialized_events = "".join(event.model_dump_json() for event in telemetry.events)
    assert secret_prompt not in serialized_events
    assert secret_tool_input not in serialized_events


def test_streamed_content_never_persisted_to_telemetry(
    monkeypatch: pytest.MonkeyPatch, anthropic_config: InferrailConfig
) -> None:
    telemetry = InMemoryTelemetrySink()
    secret = "the customer's real ssn is 000-00-0000"
    events = (
        b'event: message_start\ndata: {"type":"message_start",'
        b'"message":{"usage":{"input_tokens":1}}}\n\n'
        b'event: content_block_delta\ndata: {"type":"content_block_delta",'
        b'"index":0,"delta":{"type":"text_delta","text":"' + secret.encode() + b'"}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta",'
        b'"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":9}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    provider = AnthropicFakeProvider(stream_outcomes=[StreamScript(chunks=[events])])
    client = _make_anthropic_client(monkeypatch, anthropic_config, provider, telemetry)

    with client.stream("POST", "/v1/messages", json=_messages_body(stream=True)) as response:
        list(response.iter_bytes())

    serialized_events = "".join(event.model_dump_json() for event in telemetry.events)
    assert secret not in serialized_events
    assert telemetry.events[0].completion_tokens == 9
    assert telemetry.events[0].prompt_tokens == 1
