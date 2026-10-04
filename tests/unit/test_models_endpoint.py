"""`GET /v1/models`: route names plus, in pass-through mode, the default
provider's own model list, so clients that fill a model picker from it
(chat UIs, IDE assistants) work against the gateway unchanged."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from inferrail.config.models import InferrailConfig
from inferrail.gateway import app as app_module
from inferrail.providers.openai import OpenAIProvider


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
    monkeypatch: pytest.MonkeyPatch,
    config: InferrailConfig,
    handler: Any,
    api_key: str = "k",
) -> tuple[TestClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        result: httpx.Response = handler(request)
        return result

    provider = OpenAIProvider(
        name="openai",
        api_key=api_key,
        base_url="https://api.openai.com/v1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(record)),
        is_verified_openai=True,
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})
    return TestClient(app_module.create_app(config)), seen


def _upstream_list(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"object": "list", "data": [
            {"id": "gpt-4.1-mini", "object": "model"},
            {"id": "gpt-4o-mini", "object": "model"},
        ]},
    )


def _ids(response: httpx.Response) -> list[str]:
    return [m["id"] for m in response.json()["data"]]


def test_routes_are_listed_without_calling_upstream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, seen = _client(monkeypatch, _config(tmp_path), _upstream_list)

    response = client.get("/v1/models")

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert body["data"] == [
        {"id": "default", "object": "model", "created": 0, "owned_by": "openai"}
    ]
    assert seen == []


def test_pass_through_mode_adds_the_providers_own_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, seen = _client(
        monkeypatch, _config(tmp_path, default_provider="openai"), _upstream_list
    )

    response = client.get("/v1/models")

    assert response.status_code == 200
    assert _ids(response) == ["default", "gpt-4.1-mini", "gpt-4o-mini"]
    [request] = seen
    assert request.method == "GET"
    assert str(request.url) == "https://api.openai.com/v1/models"
    assert request.headers["authorization"] == "Bearer k"


def test_upstream_list_failure_falls_back_to_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _ = _client(
        monkeypatch,
        _config(tmp_path, default_provider="openai"),
        lambda request: httpx.Response(404, json={"error": "no such endpoint"}),
    )

    response = client.get("/v1/models")

    assert response.status_code == 200
    assert _ids(response) == ["default"]


def test_missing_provider_key_falls_back_to_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, seen = _client(
        monkeypatch, _config(tmp_path, default_provider="openai"), _upstream_list, api_key=""
    )

    response = client.get("/v1/models")

    assert response.status_code == 200
    assert _ids(response) == ["default"]
    assert seen == []


def test_gateway_token_is_enforced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("INFERRAIL_GATEWAY_TOKEN", "secret")
    client, _ = _client(monkeypatch, _config(tmp_path), _upstream_list)

    assert client.get("/v1/models").status_code == 401
    ok = client.get("/v1/models", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200
