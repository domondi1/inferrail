from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from inferrail.config.models import InferrailConfig
from inferrail.errors import AuthenticationError, RateLimitError
from inferrail.gateway import app as app_module
from inferrail.providers.base import NormalizedChatRequest, NormalizedChatResponse
from inferrail.telemetry.events import InferenceEvent


class FakeProvider:
    """A `Provider` test double: no network, fully scriptable outcomes."""

    name = "openai"

    def __init__(self, outcomes: list[Any] | None = None) -> None:
        # Each entry is either a NormalizedChatResponse to return, or an
        # exception instance to raise. Defaults to one canned success.
        self._outcomes = outcomes or [
            NormalizedChatResponse(
                content="ok", finish_reason="stop", prompt_tokens=3, completion_tokens=2
            )
        ]
        self.calls: list[NormalizedChatRequest] = []

    async def complete(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> NormalizedChatResponse:
        self.calls.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class InMemoryTelemetrySink:
    def __init__(self) -> None:
        self.events: list[InferenceEvent] = []

    def emit(self, event: InferenceEvent) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def _no_gateway_token_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate tests from whatever the host shell happens to have set, so
    # "no token configured" is a reliable baseline regardless of environment.
    monkeypatch.delenv("INFERRAIL_GATEWAY_TOKEN", raising=False)


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    config: InferrailConfig,
    provider: FakeProvider,
    telemetry: InMemoryTelemetrySink | None = None,
) -> TestClient:
    monkeypatch.setattr(app_module, "build_providers", lambda cfg: {"openai": provider})
    if telemetry is not None:
        monkeypatch.setattr(app_module, "build_telemetry_sink", lambda cfg: telemetry)
    app = app_module.create_app(config)
    return TestClient(app)


def _chat_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "default",
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


def test_health(monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig) -> None:
    client = _make_client(monkeypatch, base_config, FakeProvider())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_chat_completions_success(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    client = _make_client(monkeypatch, base_config, FakeProvider())

    response = client.post("/v1/chat/completions", json=_chat_body())

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "ok"
    assert body["usage"]["total_tokens"] == 5
    assert body["inferrail"]["route"] == "default"
    assert body["inferrail"]["provider"] == "openai"
    assert body["inferrail"]["retry_count"] == 0


def test_chat_completions_unknown_route(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    client = _make_client(monkeypatch, base_config, FakeProvider())

    response = client.post("/v1/chat/completions", json=_chat_body(model="nope"))

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "RoutingError"


def test_chat_completions_authentication_error_maps_to_401(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    provider = FakeProvider(outcomes=[AuthenticationError("bad key", provider="openai")])
    client = _make_client(monkeypatch, base_config, provider)

    response = client.post("/v1/chat/completions", json=_chat_body())

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "AuthenticationError"


def test_chat_completions_stream_unsupported(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    client = _make_client(monkeypatch, base_config, FakeProvider())

    response = client.post("/v1/chat/completions", json=_chat_body(stream=True))

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "UnsupportedFeatureError"


def test_chat_completions_retries_transient_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, base_config_dict: dict[str, Any]
) -> None:
    base_config_dict["routes"]["default"]["max_retries"] = 1
    config = InferrailConfig.model_validate(base_config_dict)
    monkeypatch.setattr("inferrail.gateway.execution._RETRY_BACKOFF_BASE_SECONDS", 0.01)

    provider = FakeProvider(
        outcomes=[
            RateLimitError("slow down", provider="openai"),
            NormalizedChatResponse(
                content="recovered", finish_reason="stop", prompt_tokens=1, completion_tokens=1
            ),
        ]
    )
    client = _make_client(monkeypatch, config, provider)

    response = client.post("/v1/chat/completions", json=_chat_body())

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "recovered"
    assert body["inferrail"]["retry_count"] == 1
    assert len(provider.calls) == 2


def test_telemetry_emitted_on_success(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    telemetry = InMemoryTelemetrySink()
    client = _make_client(monkeypatch, base_config, FakeProvider(), telemetry)

    client.post("/v1/chat/completions", json=_chat_body())

    assert len(telemetry.events) == 1
    event = telemetry.events[0]
    assert event.status == "success"
    assert event.route == "default"
    assert event.prompt_tokens == 3
    assert event.completion_tokens == 2


def test_telemetry_emitted_on_error(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    telemetry = InMemoryTelemetrySink()
    provider = FakeProvider(outcomes=[AuthenticationError("bad key", provider="openai")])
    client = _make_client(monkeypatch, base_config, provider, telemetry)

    client.post("/v1/chat/completions", json=_chat_body())

    assert len(telemetry.events) == 1
    event = telemetry.events[0]
    assert event.status == "error"
    assert event.error_category == "authentication"


def test_chat_completions_no_auth_required_by_default(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    client = _make_client(monkeypatch, base_config, FakeProvider())

    response = client.post("/v1/chat/completions", json=_chat_body())

    assert response.status_code == 200


def test_chat_completions_rejects_missing_token_when_configured(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    monkeypatch.setenv("INFERRAIL_GATEWAY_TOKEN", "s3cret")
    client = _make_client(monkeypatch, base_config, FakeProvider())

    response = client.post("/v1/chat/completions", json=_chat_body())

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "GatewayAuthenticationError"


def test_chat_completions_rejects_wrong_token_when_configured(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    monkeypatch.setenv("INFERRAIL_GATEWAY_TOKEN", "s3cret")
    client = _make_client(monkeypatch, base_config, FakeProvider())

    response = client.post(
        "/v1/chat/completions",
        json=_chat_body(),
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401


def test_chat_completions_accepts_matching_token_when_configured(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    monkeypatch.setenv("INFERRAIL_GATEWAY_TOKEN", "s3cret")
    client = _make_client(monkeypatch, base_config, FakeProvider())

    response = client.post(
        "/v1/chat/completions",
        json=_chat_body(),
        headers={"Authorization": "Bearer s3cret"},
    )

    assert response.status_code == 200


def test_health_does_not_require_token_when_configured(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    monkeypatch.setenv("INFERRAIL_GATEWAY_TOKEN", "s3cret")
    client = _make_client(monkeypatch, base_config, FakeProvider())

    response = client.get("/health")

    assert response.status_code == 200


def test_telemetry_never_persists_prompt_content_by_default(
    monkeypatch: pytest.MonkeyPatch, base_config_dict: dict[str, Any], tmp_path: Path
) -> None:
    secret = "TOP-SECRET-PROMPT-CONTENT"
    jsonl_path = tmp_path / "telemetry.jsonl"
    base_config_dict["telemetry"] = {"sink": "jsonl", "path": str(jsonl_path)}
    config = InferrailConfig.model_validate(base_config_dict)
    provider = FakeProvider(
        outcomes=[
            NormalizedChatResponse(
                content=secret, finish_reason="stop", prompt_tokens=1, completion_tokens=1
            )
        ]
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg: {"openai": provider})
    app = app_module.create_app(config)
    client = TestClient(app)

    client.post(
        "/v1/chat/completions",
        json=_chat_body(messages=[{"role": "user", "content": secret}]),
    )

    raw = jsonl_path.read_text()
    assert secret not in raw
    event = json.loads(raw.strip())
    assert event["route"] == "default"
