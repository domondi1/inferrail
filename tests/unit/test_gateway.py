from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from _fakes import FakeProvider, StreamScript
from fastapi.testclient import TestClient

from inferrail.config.models import InferrailConfig
from inferrail.errors import AuthenticationError, InvalidRequestError, RateLimitError
from inferrail.gateway import app as app_module
from inferrail.providers.base import NormalizedChatResponse
from inferrail.telemetry.events import InferenceEvent

__all__ = ["FakeProvider", "StreamScript"]  # re-exported for readability at call sites


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
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
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


def test_chat_completions_passthrough_unmatched_model(
    monkeypatch: pytest.MonkeyPatch, base_config_dict: dict[str, Any]
) -> None:
    """A model name that matches no configured route still succeeds, end to
    end, when `default_provider` is set — see
    docs/adr/0007-model-passthrough-routing.md."""
    config = InferrailConfig.model_validate(
        {**base_config_dict, "default_provider": "openai"}
    )
    client = _make_client(monkeypatch, config, FakeProvider())

    response = client.post("/v1/chat/completions", json=_chat_body(model="gpt-5.6-sol"))

    assert response.status_code == 200
    body = response.json()
    # The exact model requested — forwarded unchanged, not translated.
    assert body["model"] == "gpt-5.6-sol"
    assert body["inferrail"]["route"] == "passthrough"
    assert body["inferrail"]["provider"] == "openai"


def test_chat_completions_authentication_error_maps_to_401(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    provider = FakeProvider(outcomes=[AuthenticationError("bad key", provider="openai")])
    client = _make_client(monkeypatch, base_config, provider)

    response = client.post("/v1/chat/completions", json=_chat_body())

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "AuthenticationError"


def test_chat_completions_n_not_one_still_rejected(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    client = _make_client(monkeypatch, base_config, FakeProvider())

    response = client.post("/v1/chat/completions", json=_chat_body(n=2))

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "UnsupportedFeatureError"


def test_chat_completions_stream_forwards_raw_bytes(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(
                chunks=[
                    b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
                    b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
                    b"data: [DONE]\n\n",
                ]
            )
        ]
    )
    client = _make_client(monkeypatch, base_config, provider)

    with client.stream("POST", "/v1/chat/completions", json=_chat_body(stream=True)) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = b"".join(response.iter_bytes())

    assert body == (
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        b"data: [DONE]\n\n"
    )


def test_chat_completions_tool_call_round_trip(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    provider = FakeProvider(
        outcomes=[
            NormalizedChatResponse(
                content=None,
                finish_reason="tool_calls",
                prompt_tokens=10,
                completion_tokens=6,
                tool_calls=[
                    {
                        "id": "call_abc123",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
                    }
                ],
            )
        ]
    )
    client = _make_client(monkeypatch, base_config, provider)

    response = client.post(
        "/v1/chat/completions",
        json=_chat_body(
            tools=[
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
            ]
        ),
    )

    assert response.status_code == 200
    body = response.json()
    message = body["choices"][0]["message"]
    assert message["content"] is None
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert message["tool_calls"] == [
        {
            "id": "call_abc123",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
        }
    ]
    # The passthrough tool definition reached the provider unmodified.
    assert provider.calls[0].tools == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]


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
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
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


def test_telemetry_never_persists_provider_echoed_error_text(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    # Simulates a provider that echoes request content in its error body
    # (e.g. a content-moderation rejection quoting the flagged text) —
    # the raw text must never reach telemetry, only the sanitized summary.
    telemetry = InMemoryTelemetrySink()
    echoed_content = "TOP-SECRET-PROMPT-CONTENT-flagged-by-moderation"
    provider = FakeProvider(
        outcomes=[
            InvalidRequestError(
                f"provider 'openai' returned HTTP 400: content policy violation "
                f"for input '{echoed_content}'",
                provider="openai",
                status_code=400,
                safe_summary="provider 'openai' returned HTTP 400 (content_policy_violation)",
            )
        ]
    )
    client = _make_client(monkeypatch, base_config, provider, telemetry)

    client.post("/v1/chat/completions", json=_chat_body())

    assert len(telemetry.events) == 1
    assert echoed_content not in telemetry.events[0].error_message
    assert telemetry.events[0].error_message == (
        "provider 'openai' returned HTTP 400 (content_policy_violation)"
    )


def test_operator_log_never_contains_provider_echoed_error_text(
    monkeypatch: pytest.MonkeyPatch,
    base_config: InferrailConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Adversarial: a provider whose error body echoes back caller-submitted
    # content (e.g. a content-moderation rejection quoting the flagged
    # text). That echoed text must never reach an Inferrail-owned log —
    # only the fail-closed `safe_summary` may appear in the operator log
    # line emitted by the InferrailError exception handler (gateway/app.py).
    canary = "CANARY-9f3a1c-do-not-leak-into-operator-logs"
    provider = FakeProvider(
        outcomes=[
            InvalidRequestError(
                f"provider 'openai' returned HTTP 400: content policy violation "
                f"for input '{canary}'",
                provider="openai",
                status_code=400,
                safe_summary="provider 'openai' returned HTTP 400 (content_policy_violation)",
            )
        ]
    )
    client = _make_client(monkeypatch, base_config, provider)

    with caplog.at_level(logging.WARNING, logger="inferrail.gateway"):
        response = client.post("/v1/chat/completions", json=_chat_body())

    # The caller-facing HTTP response is a deliberately separate case: it
    # goes back to the same caller whose content this is, so it may retain
    # full provider detail — this is not the leak under test.
    assert canary in response.json()["error"]["message"]

    # Nothing Inferrail logs on its own operator-facing channel may contain
    # the canary, and the safe summary must still be present so the
    # operator isn't left with no diagnostic information at all.
    operator_log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert canary not in operator_log_text
    assert "provider 'openai' returned HTTP 400 (content_policy_violation)" in operator_log_text


def test_operator_log_never_contains_echoed_text_from_a_pre_first_chunk_stream_error(
    monkeypatch: pytest.MonkeyPatch,
    base_config: InferrailConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Same adversarial shape as the non-streaming case above, but for a
    # stream=true request that fails before any chunk reaches the client
    # — this still goes through gateway/app.py's normal exception handler
    # (see gateway/execution.py's module docstring on the retry boundary),
    # so the same fail-closed guarantee must hold here too.
    sentinel = "INFERRAIL_PHASE2_PRIVACY_SENTINEL_9f8e7d"
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(
                chunks=[],
                error=InvalidRequestError(
                    f"provider 'openai' returned HTTP 400: content policy violation "
                    f"for input '{sentinel}'",
                    provider="openai",
                    status_code=400,
                    safe_summary="provider 'openai' returned HTTP 400 (content_policy_violation)",
                ),
            )
        ]
    )
    client = _make_client(monkeypatch, base_config, provider)

    with caplog.at_level(logging.WARNING, logger="inferrail.gateway"):
        response = client.post("/v1/chat/completions", json=_chat_body(stream=True))

    assert response.status_code == 400
    assert sentinel in response.json()["error"]["message"]  # same caller, full detail

    operator_log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert sentinel not in operator_log_text
    assert "provider 'openai' returned HTTP 400 (content_policy_violation)" in operator_log_text


def test_telemetry_never_persists_echoed_text_from_a_mid_stream_provider_failure(
    monkeypatch: pytest.MonkeyPatch, base_config_dict: dict[str, Any], tmp_path: Path
) -> None:
    # A provider failure discovered *after* the first chunk was already
    # forwarded is caught inside _iter_stream (never reaches the
    # gateway/app.py exception handler, since a 200 is already committed)
    # — confirm that path independently uses exc.safe_summary, not
    # str(exc), for InferenceEvent.error_message.
    sentinel = "INFERRAIL_PHASE2_PRIVACY_SENTINEL_9f8e7d"
    jsonl_path = tmp_path / "telemetry.jsonl"
    base_config_dict["telemetry"] = {"sink": "jsonl", "path": str(jsonl_path)}
    config = InferrailConfig.model_validate(base_config_dict)
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(
                chunks=[b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'],
                error=InvalidRequestError(
                    f"provider 'openai' returned HTTP 400: content policy violation "
                    f"for input '{sentinel}'",
                    provider="openai",
                    status_code=400,
                    safe_summary="provider 'openai' returned HTTP 400 (content_policy_violation)",
                ),
            )
        ]
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    app = app_module.create_app(config)
    client = TestClient(app)

    with client.stream("POST", "/v1/chat/completions", json=_chat_body(stream=True)) as response:
        list(response.iter_bytes())  # drain; the failure happens after chunk 1

    raw_telemetry = jsonl_path.read_text()
    assert sentinel not in raw_telemetry
    assert "content_policy_violation" in raw_telemetry


def test_early_exit_from_testclient_stream_still_never_persists_secret_content(
    monkeypatch: pytest.MonkeyPatch, base_config_dict: dict[str, Any], tmp_path: Path
) -> None:
    # Note on what this test can and can't prove: Starlette's TestClient
    # runs the ASGI app to completion in the background regardless of
    # whether the httpx-side caller keeps reading — exiting this `with`
    # block early does NOT propagate a real client disconnect down to
    # `_iter_stream` (confirmed empirically: the resulting status is
    # "success", not "partial"). Real mid-stream cancellation — GeneratorExit
    # reaching `_iter_stream`, `.aclose()` reaching the upstream provider —
    # is exercised directly at the engine level in
    # test_streaming.py::test_stream_cancellation_emits_partial_and_closes_upstream,
    # and was independently confirmed against a real running server
    # process in the Phase 2 report's live SDK verification. What this
    # test *does* still prove: even when TestClient drains the full
    # stream regardless of early exit, secret content never reaches local
    # persistence — the structural guarantee holds independent of how
    # the stream actually ends.
    sentinel = "INFERRAIL_PHASE2_PRIVACY_SENTINEL_9f8e7d"
    jsonl_path = tmp_path / "telemetry.jsonl"
    receipts_path = tmp_path / "receipts.jsonl"
    base_config_dict["telemetry"] = {"sink": "jsonl", "path": str(jsonl_path)}
    base_config_dict["receipts"] = {"sink": "jsonl", "path": str(receipts_path)}
    config = InferrailConfig.model_validate(base_config_dict)
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(
                chunks=[
                    f'data: {{"choices":[{{"delta":{{"content":"{sentinel}"}}}}]}}\n\n'.encode(),
                    b'data: {"choices":[{"delta":{"content":" more"}}]}\n\n',
                    b"data: [DONE]\n\n",
                ]
            )
        ]
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    app = app_module.create_app(config)
    client = TestClient(app)

    with client.stream("POST", "/v1/chat/completions", json=_chat_body(stream=True)) as response:
        it = response.iter_bytes()
        first = next(it)
        assert sentinel.encode() in first
        # Exit the `with` block without draining the rest.

    assert sentinel not in jsonl_path.read_text()
    assert sentinel not in receipts_path.read_text()


# ---------------------------------------------------------------------------
# Privacy under tool calls and streaming (Workstream 2A.8)
# ---------------------------------------------------------------------------


def test_telemetry_never_persists_tool_call_arguments(
    monkeypatch: pytest.MonkeyPatch, base_config_dict: dict[str, Any], tmp_path: Path
) -> None:
    secret = "TOP-SECRET-fake-api-key-embedded-in-a-tool-argument"
    jsonl_path = tmp_path / "telemetry.jsonl"
    receipts_path = tmp_path / "receipts.jsonl"
    base_config_dict["telemetry"] = {"sink": "jsonl", "path": str(jsonl_path)}
    base_config_dict["receipts"] = {"sink": "jsonl", "path": str(receipts_path)}
    config = InferrailConfig.model_validate(base_config_dict)
    provider = FakeProvider(
        outcomes=[
            NormalizedChatResponse(
                content=None,
                finish_reason="tool_calls",
                prompt_tokens=5,
                completion_tokens=5,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "send_request",
                            "arguments": f'{{"payload": "{secret}"}}',
                        },
                    }
                ],
            )
        ]
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    app = app_module.create_app(config)
    client = TestClient(app)

    client.post(
        "/v1/chat/completions",
        json=_chat_body(
            messages=[{"role": "user", "content": f"do something with {secret}"}],
            tools=[{"type": "function", "function": {"name": "send_request"}}],
        ),
    )

    assert secret not in jsonl_path.read_text()
    assert secret not in receipts_path.read_text()


def test_telemetry_never_persists_tool_result_content(
    monkeypatch: pytest.MonkeyPatch, base_config_dict: dict[str, Any], tmp_path: Path
) -> None:
    secret = "TOP-SECRET-database-row-returned-by-a-tool"
    jsonl_path = tmp_path / "telemetry.jsonl"
    base_config_dict["telemetry"] = {"sink": "jsonl", "path": str(jsonl_path)}
    config = InferrailConfig.model_validate(base_config_dict)
    provider = FakeProvider()
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    app = app_module.create_app(config)
    client = TestClient(app)

    client.post(
        "/v1/chat/completions",
        json=_chat_body(
            messages=[
                {"role": "user", "content": "look up the customer"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": secret},
            ]
        ),
    )

    assert secret not in jsonl_path.read_text()
    # The tool result reached the fake provider unchanged (proves the
    # request round-tripped, not just that the assertion above is vacuous).
    assert provider.calls[0].messages[-1].content == secret


def test_request_validation_error_never_touches_telemetry_or_receipts(
    monkeypatch: pytest.MonkeyPatch, base_config_dict: dict[str, Any], tmp_path: Path
) -> None:
    # A request that fails pydantic validation (here: max_tokens is typed
    # int | None, and this value can't coerce) never reaches
    # InferenceEngine at all — FastAPI's default validation handler
    # returns 422 before the route body runs. Confirm that structural
    # expectation directly: the sentinel below appears in FastAPI's own
    # validation-error response body (a standard "input") but must never
    # touch a local persistence surface, since no InferenceEvent/
    # InferenceReceipt should ever be built for a request that never
    # reached the engine.
    sentinel = "INFERRAIL_PHASE2_PRIVACY_SENTINEL_9f8e7d"
    jsonl_path = tmp_path / "telemetry.jsonl"
    receipts_path = tmp_path / "receipts.jsonl"
    base_config_dict["telemetry"] = {"sink": "jsonl", "path": str(jsonl_path)}
    base_config_dict["receipts"] = {"sink": "jsonl", "path": str(receipts_path)}
    config = InferrailConfig.model_validate(base_config_dict)
    provider = FakeProvider()
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    app = app_module.create_app(config)
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json=_chat_body(max_tokens=sentinel),  # wrong type: not coercible to int | None
    )

    assert response.status_code == 422
    assert not jsonl_path.exists()
    assert not receipts_path.exists()
    assert len(provider.calls) == 0


def test_streamed_content_never_persisted_to_telemetry_or_receipts(
    monkeypatch: pytest.MonkeyPatch, base_config_dict: dict[str, Any], tmp_path: Path
) -> None:
    secret = "TOP-SECRET-streamed-completion-content"
    jsonl_path = tmp_path / "telemetry.jsonl"
    receipts_path = tmp_path / "receipts.jsonl"
    base_config_dict["telemetry"] = {"sink": "jsonl", "path": str(jsonl_path)}
    base_config_dict["receipts"] = {"sink": "jsonl", "path": str(receipts_path)}
    config = InferrailConfig.model_validate(base_config_dict)
    chunk = (
        '{"choices":[{"delta":{"content":"' + secret + '"}}],'
        '"usage":{"prompt_tokens":1,"completion_tokens":1}}'
    )
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(chunks=[f"data: {chunk}\n\n".encode(), b"data: [DONE]\n\n"])
        ]
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    app = app_module.create_app(config)
    client = TestClient(app)

    with client.stream("POST", "/v1/chat/completions", json=_chat_body(stream=True)) as response:
        body = b"".join(response.iter_bytes())

    # The secret DOES reach the client — Inferrail proxies the stream
    # transparently — but must never land in local telemetry/receipts.
    assert secret.encode() in body
    assert secret not in jsonl_path.read_text()
    assert secret not in receipts_path.read_text()


def test_streamed_tool_call_arguments_never_persisted_to_telemetry_or_receipts(
    monkeypatch: pytest.MonkeyPatch, base_config_dict: dict[str, Any], tmp_path: Path
) -> None:
    # Distinct from the plain-content case above: the secret lives inside
    # a streamed tool_calls[].function.arguments fragment, not
    # delta.content — same raw-passthrough mechanism, exercised
    # specifically to confirm it applies to tool-call argument fragments
    # too, not just ordinary assistant text.
    secret = "TOP-SECRET-embedded-in-a-streamed-tool-argument-fragment"
    jsonl_path = tmp_path / "telemetry.jsonl"
    receipts_path = tmp_path / "receipts.jsonl"
    base_config_dict["telemetry"] = {"sink": "jsonl", "path": str(jsonl_path)}
    base_config_dict["receipts"] = {"sink": "jsonl", "path": str(receipts_path)}
    config = InferrailConfig.model_validate(base_config_dict)
    delta_chunk = (
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
        '"type":"function","function":{"name":"send","arguments":"' + secret + '"}}]}}]}'
    )
    final_chunk = (
        '{"choices":[{"delta":{},"finish_reason":"tool_calls"}],'
        '"usage":{"prompt_tokens":5,"completion_tokens":5}}'
    )
    provider = FakeProvider(
        stream_outcomes=[
            StreamScript(
                chunks=[
                    f"data: {delta_chunk}\n\n".encode(),
                    f"data: {final_chunk}\n\n".encode(),
                    b"data: [DONE]\n\n",
                ]
            )
        ]
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    app = app_module.create_app(config)
    client = TestClient(app)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json=_chat_body(
            stream=True, tools=[{"type": "function", "function": {"name": "send"}}]
        ),
    ) as response:
        body = b"".join(response.iter_bytes())

    assert secret.encode() in body  # reaches the client, unmodified
    assert secret not in jsonl_path.read_text()
    assert secret not in receipts_path.read_text()
