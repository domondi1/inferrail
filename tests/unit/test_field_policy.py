"""The `/v1/chat/completions` request-field policy — see
docs/adr/0021-atomic-budget-reservations.md ("Request-field policy").

Three explicit lists: fields Inferrail interprets, provider-valid fields
it forwards unchanged, and fields it rejects with a stated reason.
Anything else is still rejected (`INFERRAIL_E006`) — never blind
pass-through, never silently dropped.

Runs through the real FastAPI app and a real `OpenAIProvider` whose
HTTP transport is mocked, so every assertion is about the exact JSON
that would have gone upstream.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from inferrail.budgets.schema import Budget, new_budget_id
from inferrail.budgets.store import BudgetStore
from inferrail.config.models import InferrailConfig
from inferrail.gateway import app as app_module
from inferrail.providers.openai import OpenAIProvider

CANARY = "CANARY-4be1-forwarded-field-must-not-be-stored"


class Upstream:
    """Records every JSON body the gateway sends upstream."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.bodies: list[dict[str, Any]] = []
        self._response = response or {
            "id": "chatcmpl-1",
            "model": "gpt-4o-mini",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json=self._response)


@pytest.fixture(autouse=True)
def _no_gateway_token_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFERRAIL_GATEWAY_TOKEN", raising=False)


def _config(tmp_path: Path) -> InferrailConfig:
    return InferrailConfig.model_validate(
        {
            "providers": {"openai": {"type": "openai", "api_key_env": "TEST_OPENAI_API_KEY"}},
            "routes": {"default": {"provider": "openai", "model": "gpt-4o-mini"}},
            "telemetry": {"sink": "jsonl", "path": str(tmp_path / "telemetry.jsonl")},
            "receipts": {"sink": "sqlite", "path": str(tmp_path / "receipts.db")},
            "budgets": {"enabled": True, "path": str(tmp_path / "budgets.db")},
        }
    )


def _client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, upstream: Upstream
) -> TestClient:
    provider = OpenAIProvider(
        name="openai",
        api_key="k",
        base_url="https://api.openai.com/v1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
        is_verified_openai=True,
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})
    return TestClient(app_module.create_app(_config(tmp_path)))


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "default",
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


FORWARDED: dict[str, Any] = {
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"value": {"type": "string", "description": CANARY}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    },
    "seed": 7,
    "max_completion_tokens": 256,
    "frequency_penalty": 0.1,
    "presence_penalty": 0.2,
    "logit_bias": {"50256": -100},
    "metadata": {"note": CANARY},
    "store": False,
    "reasoning_effort": "low",
    "verbosity": "low",
    "prediction": {"type": "content", "content": CANARY},
    "prompt_cache_key": "cache-key-1",
    "prompt_cache_retention": "24h",
    "prompt_cache_options": {"mode": "auto"},
    "safety_identifier": "user-hash-1",
    "service_tier": "auto",
}


def test_provider_valid_fields_are_forwarded_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, tmp_path, upstream)

    response = client.post("/v1/chat/completions", json=_body(**FORWARDED))

    assert response.status_code == 200, response.text
    [sent] = upstream.bodies
    for field, value in FORWARDED.items():
        assert sent[field] == value, field


def test_forwarded_field_content_is_never_stored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Payload-free canary: forwarding a field must not mean persisting it."""
    upstream = Upstream()
    client = _client(monkeypatch, tmp_path, upstream)

    response = client.post(
        "/v1/chat/completions",
        json=_body(**FORWARDED),
        headers={"X-Inferrail-Attribute-Work-Id": "run-1"},
    )
    assert response.status_code == 200

    for stored in tmp_path.iterdir():
        assert CANARY.encode() not in stored.read_bytes(), stored.name


def test_unmodeled_unknown_field_is_still_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, tmp_path, upstream)

    response = client.post("/v1/chat/completions", json=_body(totally_made_up_field=1))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INFERRAIL_E006"
    assert "totally_made_up_field" in response.json()["error"]["message"]
    assert upstream.bodies == []


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("audio", {"voice": "alloy", "format": "wav"}, "audio"),
        ("modalities", ["text", "audio"], "audio"),
        ("web_search_options", {}, "search"),
        ("logprobs", True, "logprobs"),
        ("top_logprobs", 2, "logprobs"),
        ("functions", [{"name": "f", "parameters": {}}], "tools"),
        ("function_call", "auto", "tool_choice"),
    ],
)
def test_accounting_incompatible_fields_are_rejected_with_a_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str, value: Any, reason: str
) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, tmp_path, upstream)

    response = client.post("/v1/chat/completions", json=_body(**{field: value}))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "INFERRAIL_E006"
    assert field in error["message"]
    assert reason in error["message"]
    assert upstream.bodies == []


@pytest.mark.parametrize("tier", ["flex", "priority", "scale"])
def test_non_default_service_tier_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tier: str
) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, tmp_path, upstream)

    response = client.post("/v1/chat/completions", json=_body(service_tier=tier))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INFERRAIL_E006"
    assert "service_tier" in response.json()["error"]["message"]
    assert upstream.bodies == []


def test_agent_framework_message_shapes_are_forwarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, tmp_path, upstream)
    messages = [
        {"role": "developer", "content": "Be terse."},
        {"role": "user", "name": "alice", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": None,
            "refusal": "I can't help with that.",
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "calling"}],
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "lookup", "arguments": "{\"q\":1}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "42"},
    ]

    response = client.post("/v1/chat/completions", json=_body(messages=messages))

    assert response.status_code == 200, response.text
    [sent] = upstream.bodies
    assert sent["messages"] == messages[:2] + [
        {"role": "assistant", "refusal": "I can't help with that."}
    ] + messages[3:]


def test_non_text_content_part_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, tmp_path, upstream)
    content = [{"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}}]

    response = client.post(
        "/v1/chat/completions", json=_body(messages=[{"role": "user", "content": content}])
    )

    assert response.status_code in (400, 422)
    assert upstream.bodies == []


def test_unknown_message_key_is_rejected_not_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, tmp_path, upstream)

    response = client.post(
        "/v1/chat/completions",
        json=_body(messages=[{"role": "user", "content": "hi", "mystery": 1}]),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INFERRAIL_E006"
    assert "mystery" in response.json()["error"]["message"]
    assert upstream.bodies == []


def test_refusal_is_returned_on_a_non_streaming_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = Upstream(
        {
            "id": "chatcmpl-2",
            "model": "gpt-4o-mini",
            "choices": [
                {"index": 0,
                 "message": {"role": "assistant", "content": None, "refusal": "No."},
                 "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
    )
    client = _client(monkeypatch, tmp_path, upstream)

    response = client.post("/v1/chat/completions", json=_body())

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["refusal"] == "No."


def test_max_completion_tokens_bounds_the_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    BudgetStore(config.budgets.path).set(
        Budget(
            budget_id=new_budget_id("work_id", "run-1", "per_work"), scope="work_id",
            scope_value="run-1", window="per_work", mode="block",
            limit_usd="0.0001",  # type: ignore[arg-type]
        )
    )
    upstream = Upstream()
    client = _client(monkeypatch, tmp_path, upstream)
    headers = {"X-Inferrail-Attribute-Work-Id": "run-1"}

    # 16 output tokens of gpt-4o-mini fit well under $0.0001 ...
    small = client.post(
        "/v1/chat/completions", json=_body(max_completion_tokens=16), headers=headers
    )
    # ... 100k do not.
    large = client.post(
        "/v1/chat/completions", json=_body(max_completion_tokens=100_000), headers=headers
    )

    assert small.status_code == 200, small.text
    assert large.status_code == 402
    assert len(upstream.bodies) == 1
