"""End-to-end (through the real FastAPI app, via TestClient) tests for
budget enforcement — see docs/adr/0015-budget-enforcement.md.

Covers both wire-format engines (`/v1/chat/completions` and
`/v1/messages`) since `BudgetEnforcer` is shared between them, exactly
like receipts/telemetry/pricing already are (ADR-0014).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from _fakes import AnthropicFakeProvider, FakeProvider
from fastapi.testclient import TestClient

from inferrail.budgets.schema import Budget, new_budget_id
from inferrail.budgets.store import BudgetStore
from inferrail.config.models import InferrailConfig
from inferrail.gateway import app as app_module
from inferrail.providers.base import NormalizedChatResponse
from inferrail.receipts.sqlite_store import ReceiptsStore


@pytest.fixture(autouse=True)
def _no_gateway_token_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFERRAIL_GATEWAY_TOKEN", raising=False)


def _config_with_budgets(tmp_path: Path) -> InferrailConfig:
    return InferrailConfig.model_validate(
        {
            "providers": {
                "openai": {"type": "openai", "api_key_env": "TEST_OPENAI_API_KEY"},
                "anthropic": {"type": "anthropic", "api_key_env": "TEST_ANTHROPIC_API_KEY"},
            },
            "routes": {
                "default": {"provider": "openai", "model": "gpt-4o-mini"},
                "claude": {"provider": "anthropic", "model": "claude-sonnet-5"},
            },
            "telemetry": {"sink": "none"},
            "receipts": {"sink": "sqlite", "path": str(tmp_path / "receipts.db")},
            "budgets": {"enabled": True, "path": str(tmp_path / "budgets.db")},
        }
    )


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    config: InferrailConfig,
    openai_provider: FakeProvider | None = None,
    anthropic_provider: AnthropicFakeProvider | None = None,
) -> TestClient:
    monkeypatch.setattr(
        app_module, "build_providers",
        lambda cfg, **_kw: {"openai": openai_provider} if openai_provider else {},
    )
    monkeypatch.setattr(
        app_module, "build_anthropic_providers",
        lambda cfg, **_kw: {"anthropic": anthropic_provider} if anthropic_provider else {},
    )
    app = app_module.create_app(config)
    return TestClient(app)


def _chat_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "default",
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


def _messages_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "claude",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


def test_block_mode_budget_blocks_chat_completion_before_provider_is_called(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config_with_budgets(tmp_path)
    BudgetStore(config.budgets.path).set(
        Budget(
            budget_id=new_budget_id("global", None, "daily"), scope="global",
            window="daily", mode="block", limit_usd=Decimal("0.0001"),
        )
    )
    provider = FakeProvider()
    client = _make_client(monkeypatch, config, openai_provider=provider)

    # No max_tokens -> the OpenAI-side fallback estimate (4096 tokens)
    # applies, which comfortably exceeds a $0.0001 cap for gpt-4o-mini.
    response = client.post("/v1/chat/completions", json=_chat_body())

    assert response.status_code == 402
    body = response.json()
    assert body["error"]["code"] == "INFERRAIL_E010"
    assert body["error"]["details"]["budget_id"] == "global:_:daily"
    assert body["error"]["details"]["mode"] == "block"
    assert provider.calls == []  # never contacted


def test_blocked_request_is_visible_in_the_receipts_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config_with_budgets(tmp_path)
    BudgetStore(config.budgets.path).set(
        Budget(
            budget_id=new_budget_id("global", None, "daily"), scope="global",
            window="daily", mode="block", limit_usd=Decimal("0.0001"),
        )
    )
    client = _make_client(monkeypatch, config, openai_provider=FakeProvider())

    client.post("/v1/chat/completions", json=_chat_body())

    receipts = ReceiptsStore(config.receipts.path).read_all()[0]
    assert len(receipts) == 1
    assert receipts[0].status == "error"
    assert receipts[0].prompt_tokens is None
    assert receipts[0].estimated_cost_usd is None
    # The dashboard's Budgets screen (docs/adr/0017) filters on this to
    # build an honest blocked-request log -- distinguishing a real budget
    # block from any other status="error" receipt.
    assert receipts[0].attributes["budget_id"] == "global:_:daily"


def test_warn_mode_budget_never_blocks_chat_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config_with_budgets(tmp_path)
    BudgetStore(config.budgets.path).set(
        Budget(
            budget_id=new_budget_id("global", None, "daily"), scope="global",
            window="daily", mode="warn", limit_usd=Decimal("0.01"),
        )
    )
    provider = FakeProvider()
    client = _make_client(monkeypatch, config, openai_provider=provider)

    response = client.post("/v1/chat/completions", json=_chat_body())

    assert response.status_code == 200
    assert len(provider.calls) == 1


def test_warn_mode_overrun_is_recorded_on_the_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config_with_budgets(tmp_path)
    BudgetStore(config.budgets.path).set(
        Budget(
            budget_id=new_budget_id("global", None, "daily"), scope="global",
            window="daily", mode="warn", limit_usd=Decimal("0.000001"),
        )
    )
    client = _make_client(monkeypatch, config, openai_provider=FakeProvider())

    client.post("/v1/chat/completions", json=_chat_body())

    receipts = ReceiptsStore(config.receipts.path).read_all()[0]
    assert len(receipts) == 1
    assert "budget_overrun_usd" in receipts[0].attributes
    assert Decimal(receipts[0].attributes["budget_overrun_usd"]) > 0


def test_budget_without_matching_scope_never_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config_with_budgets(tmp_path)
    BudgetStore(config.budgets.path).set(
        Budget(
            budget_id=new_budget_id("project", "acme", "monthly"), scope="project",
            scope_value="acme", window="monthly", mode="block", limit_usd=Decimal("0.01"),
        )
    )
    provider = FakeProvider()
    client = _make_client(monkeypatch, config, openai_provider=provider)

    response = client.post(
        "/v1/chat/completions",
        json=_chat_body(),
        headers={"X-Inferrail-Attribute-Project": "someone-else"},
    )

    assert response.status_code == 200
    assert len(provider.calls) == 1


def test_no_budgets_configured_is_a_full_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config_with_budgets(tmp_path)  # enabled, but nothing set yet
    provider = FakeProvider()
    client = _make_client(monkeypatch, config, openai_provider=provider)

    response = client.post("/v1/chat/completions", json=_chat_body())

    assert response.status_code == 200
    assert len(provider.calls) == 1


def test_block_mode_budget_blocks_messages_before_provider_is_called(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config_with_budgets(tmp_path)
    BudgetStore(config.budgets.path).set(
        Budget(
            budget_id=new_budget_id("global", None, "daily"), scope="global",
            window="daily", mode="block", limit_usd=Decimal("0.01"),
        )
    )
    provider = AnthropicFakeProvider()
    client = _make_client(monkeypatch, config, anthropic_provider=provider)

    response = client.post("/v1/messages", json=_messages_body(max_tokens=1_000_000))

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "INFERRAIL_E010"
    assert provider.calls == []
    receipts = ReceiptsStore(config.receipts.path).read_all()[0]
    assert receipts[0].attributes["budget_id"] == "global:_:daily"


def test_work_id_scoped_budget_blocks_only_that_work_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config_with_budgets(tmp_path)
    BudgetStore(config.budgets.path).set(
        Budget(
            budget_id=new_budget_id("work_id", "wf_1", "per_work"), scope="work_id",
            scope_value="wf_1", window="per_work", mode="block", limit_usd=Decimal("0.0001"),
        )
    )
    provider = FakeProvider(
        outcomes=[
            NormalizedChatResponse(
                content="ok", finish_reason="stop", prompt_tokens=3, completion_tokens=2
            )
        ]
    )
    client = _make_client(monkeypatch, config, openai_provider=provider)

    blocked = client.post(
        "/v1/chat/completions",
        json=_chat_body(),
        headers={"X-Inferrail-Attribute-Work-Id": "wf_1"},
    )
    allowed = client.post(
        "/v1/chat/completions",
        json=_chat_body(),
        headers={"X-Inferrail-Attribute-Work-Id": "wf_2"},
    )

    assert blocked.status_code == 402
    assert allowed.status_code == 200
