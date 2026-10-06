"""`work_id_headers`: take a request's work id from a header an agent
already sends (e.g. Claude Code's `X-Claude-Code-Session-Id`), so each
session gets its own work id and per-run budget without a wrapper."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from inferrail.budgets.store import BudgetStore
from inferrail.config.models import InferrailConfig
from inferrail.gateway import app as app_module
from inferrail.gateway.attribution import extract_attributes
from inferrail.providers.openai import OpenAIProvider
from inferrail.receipts.sqlite_store import ReceiptsStore

SESSION = "X-Claude-Code-Session-Id"


def test_header_supplies_work_id_case_insensitively() -> None:
    attrs = extract_attributes({"x-claude-code-session-id": "s-1"}, [SESSION])
    assert attrs == {"work_id": "s-1"}


def test_explicit_work_id_header_wins() -> None:
    attrs = extract_attributes(
        {"X-Inferrail-Attribute-Work-Id": "run-7", SESSION: "s-1"}, [SESSION]
    )
    assert attrs["work_id"] == "run-7"


def test_first_present_configured_header_wins_and_empty_is_skipped() -> None:
    headers = {"x-opencode-parent-session-id": "", "x-opencode-session-id": "child"}
    attrs = extract_attributes(
        headers, ["x-opencode-parent-session-id", "x-opencode-session-id"]
    )
    assert attrs == {"work_id": "child"}


def test_nothing_is_inferred_without_configuration() -> None:
    assert extract_attributes({SESSION: "s-1"}) == {}


class _Upstream:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "c", "model": "gpt-4o-mini",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )


@pytest.fixture(autouse=True)
def _no_gateway_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFERRAIL_GATEWAY_TOKEN", raising=False)


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, upstream: _Upstream) -> TestClient:
    config = InferrailConfig.model_validate(
        {
            "providers": {"openai": {"type": "openai", "api_key_env": "TEST_OPENAI_API_KEY"}},
            "routes": {"default": {"provider": "openai", "model": "gpt-4o-mini"}},
            "telemetry": {"sink": "none"},
            "receipts": {"sink": "sqlite", "path": str(tmp_path / "receipts.db")},
            "budgets": {"enabled": True, "path": str(tmp_path / "budgets.db")},
            "work_id_headers": [SESSION],
        }
    )
    provider = OpenAIProvider(
        name="openai", api_key="k", base_url="https://api.openai.com/v1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
        is_verified_openai=True,
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})
    return TestClient(app_module.create_app(config))


def _post(client: TestClient, session: str, budget: str) -> Any:
    return client.post(
        "/v1/chat/completions",
        json={
            "model": "default",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={SESSION: session, "X-Inferrail-Budget-Usd": budget},
    )


def test_each_session_gets_its_own_budget_and_receipts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = _Upstream()
    client = _client(monkeypatch, tmp_path, upstream)

    assert _post(client, "session-a", "0.01").status_code == 200
    assert _post(client, "session-b", "0.01").status_code == 200

    budgets = {b.budget_id: b.limit_usd for b in BudgetStore(tmp_path / "budgets.db").list()}
    assert budgets == {
        "work_id:session-a:per_work": Decimal("0.01"),
        "work_id:session-b:per_work": Decimal("0.01"),
    }
    receipts, _ = ReceiptsStore(tmp_path / "receipts.db").read_all()
    assert sorted(r.attributes["work_id"] for r in receipts) == [
        "session-a", "session-b",
    ]
    assert all(SESSION.lower() not in {k.lower() for k in r.headers} for r in upstream.requests)


def test_a_session_past_its_budget_is_refused_before_the_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = _Upstream()
    client = _client(monkeypatch, tmp_path, upstream)

    # 1000 max tokens of gpt-4o-mini output alone is $0.0006 > $0.0001.
    response = _post(client, "session-a", "0.0001")

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "INFERRAIL_E010"
    assert upstream.requests == []
