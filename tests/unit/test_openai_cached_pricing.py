"""OpenAI prompt-cache pricing: prompt tokens the provider served from its
cache (`usage.prompt_tokens_details.cached_tokens`) are billed at the
model's "Cached input" rate, not the full input rate.

Runs through the real FastAPI app and a real `OpenAIProvider` whose HTTP
transport is mocked, then reads the receipt the gateway wrote."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from inferrail.config.models import InferrailConfig
from inferrail.gateway import app as app_module
from inferrail.pricing.builtin import BUILTIN_OPENAI_PRICING
from inferrail.providers.openai import OpenAIProvider

# gpt-4o-mini: $0.15 input, $0.075 cached input, $0.60 output per 1M tokens.
_USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 100,
    "prompt_tokens_details": {"cached_tokens": 800},
}
# 200 uncached x 0.15 + 800 cached x 0.075 + 100 output x 0.60, per 1M.
_CACHED_COST = Decimal("0.00015")
# The same usage with every prompt token at the full input rate.
_FULL_RATE_COST = Decimal("0.00021")


@pytest.fixture(autouse=True)
def _no_gateway_token_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFERRAIL_GATEWAY_TOKEN", raising=False)


def _config(tmp_path: Path, **overrides: Any) -> InferrailConfig:
    raw: dict[str, Any] = {
        "providers": {"openai": {"type": "openai", "api_key_env": "TEST_OPENAI_API_KEY"}},
        "routes": {"default": {"provider": "openai", "model": "gpt-4o-mini"}},
        "telemetry": {"sink": "none"},
        "receipts": {"sink": "sqlite", "path": str(tmp_path / "receipts.db")},
    }
    raw.update(overrides)
    return InferrailConfig.model_validate(raw)


def _client(
    monkeypatch: pytest.MonkeyPatch, config: InferrailConfig, handler: Any
) -> TestClient:
    provider = OpenAIProvider(
        name="openai",
        api_key="k",
        base_url="https://api.openai.com/v1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        is_verified_openai=True,
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})
    return TestClient(app_module.create_app(config))


def _completion(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "model": "gpt-4o-mini",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}
            ],
            "usage": _USAGE,
        },
    )


def _stream(_: httpx.Request) -> httpx.Response:
    chunks = [
        {"id": "c1", "object": "chat.completion.chunk", "model": "gpt-4o-mini",
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"},
                      "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "model": "gpt-4o-mini",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"id": "c1", "object": "chat.completion.chunk", "model": "gpt-4o-mini",
         "choices": [], "usage": _USAGE},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def _only_receipt(tmp_path: Path) -> sqlite3.Row:
    db = sqlite3.connect(tmp_path / "receipts.db")
    db.row_factory = sqlite3.Row
    [row] = db.execute("SELECT estimated_cost_usd, cache_read_input_tokens FROM receipts")
    return row


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"model": "default", "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return body


def test_every_builtin_openai_price_has_a_cached_input_rate() -> None:
    for model, entry in BUILTIN_OPENAI_PRICING.items():
        assert entry.cache_read_usd_per_million is not None, model
        assert entry.cache_read_usd_per_million < entry.input_usd_per_million, model
    assert BUILTIN_OPENAI_PRICING["gpt-4o-mini"].cache_read_usd_per_million == Decimal("0.075")


def test_cached_prompt_tokens_are_billed_at_the_cached_rate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client(monkeypatch, _config(tmp_path), _completion)

    response = client.post("/v1/chat/completions", json=_body())

    assert response.status_code == 200, response.text
    receipt = _only_receipt(tmp_path)
    assert Decimal(receipt["estimated_cost_usd"]) == _CACHED_COST
    assert receipt["cache_read_input_tokens"] == 800


def test_streamed_cached_prompt_tokens_are_billed_at_the_cached_rate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client(monkeypatch, _config(tmp_path), _stream)

    with client.stream("POST", "/v1/chat/completions", json=_body(stream=True)) as response:
        assert response.status_code == 200
        b"".join(response.iter_bytes())

    receipt = _only_receipt(tmp_path)
    assert Decimal(receipt["estimated_cost_usd"]) == _CACHED_COST
    assert receipt["cache_read_input_tokens"] == 800


def test_operator_price_without_a_cached_rate_keeps_the_full_input_rate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An operator `pricing:` entry that doesn't declare a cached rate keeps
    the previous behavior: cached tokens at the full input rate (overstated),
    not an unknown cost."""
    config = _config(
        tmp_path,
        pricing={"openai": {"gpt-4o-mini": {
            "input_usd_per_million": "0.15", "output_usd_per_million": "0.60",
            "source": "operator", "verified_date": "2026-10-05",
        }}},
    )
    client = _client(monkeypatch, config, _completion)

    response = client.post("/v1/chat/completions", json=_body())

    assert response.status_code == 200, response.text
    receipt = _only_receipt(tmp_path)
    assert Decimal(receipt["estimated_cost_usd"]) == _FULL_RATE_COST
    assert receipt["cache_read_input_tokens"] is None
