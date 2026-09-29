"""Per-run budgets without pre-registration — see
docs/adr/0022-per-run-budget-declaration.md.

A request carrying a `work_id` either declares a budget
(`X-Inferrail-Budget-Usd`) or inherits `budgets.per_work_default_usd`;
the per_work budget is created inside the admission transaction, so
concurrent first requests for an unseen id can't bypass it.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from _budget_harness import (
    WORK_ID,
    GatedProvider,
    make_harness,
    settle_tasks,
)
from _fakes import FakeProvider
from fastapi.testclient import TestClient

from inferrail.budgets.enforcement import BudgetEnforcer
from inferrail.budgets.store import BudgetStore
from inferrail.config.models import InferrailConfig
from inferrail.errors import BudgetDeclarationError, BudgetExceededError, BudgetUnpricedModelError
from inferrail.errors.codes import code_for
from inferrail.gateway import app as app_module
from inferrail.gateway.schemas import ChatCompletionRequest
from inferrail.providers.openai import OpenAIProvider

ATTRS = {"work_id": WORK_ID}


def _chat(model: str = "default") -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {"model": model, "messages": [{"role": "user", "content": "hello"}], "max_tokens": 1000}
    )


def _enforcer(h: Any, **kwargs: Any) -> BudgetEnforcer:
    return BudgetEnforcer(h.budgets, h.receipts, h.pricing, **kwargs)


def _reserve(enforcer: BudgetEnforcer, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "request_id": "req_test",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "attributes": ATTRS,
        "prompt_chars": 5,
        "max_completion_tokens": 1000,
    }
    kwargs.update(overrides)
    return enforcer.reserve(**kwargs)


# ---------------------------------------------------------------------------
# Enforcer: declared budgets
# ---------------------------------------------------------------------------


def test_declared_budget_is_created_on_first_use(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    enforcer = _enforcer(h)

    reservation = _reserve(enforcer, declared_limit_usd=Decimal("0.005"))

    assert reservation is not None
    [budget] = h.budgets.list()
    assert budget.budget_id == f"work_id:{WORK_ID}:per_work"
    assert budget.limit_usd == Decimal("0.005")
    assert budget.mode == "block"


def test_declared_budget_is_enforced(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    enforcer = _enforcer(h)

    _reserve(enforcer, declared_limit_usd=Decimal("0.005"))
    _reserve(enforcer, declared_limit_usd=Decimal("0.005"))
    with pytest.raises(BudgetExceededError):
        _reserve(enforcer, declared_limit_usd=Decimal("0.005"))


def test_same_declaration_again_is_accepted(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    enforcer = _enforcer(h)
    _reserve(enforcer, declared_limit_usd=Decimal("1.00"))

    # Retries and every call of the run send the same header; equal
    # amounts in different spellings are the same declaration.
    assert _reserve(enforcer, declared_limit_usd=Decimal("1.0")) is not None


def test_conflicting_declaration_is_refused(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    enforcer = _enforcer(h)
    _reserve(enforcer, declared_limit_usd=Decimal("0.50"))

    with pytest.raises(BudgetDeclarationError) as exc_info:
        _reserve(enforcer, declared_limit_usd=Decimal("5.00"))

    assert exc_info.value.reason == "conflict"
    assert code_for(exc_info.value).code == "INFERRAIL_E013"
    [budget] = h.budgets.list()
    assert budget.limit_usd == Decimal("0.50")  # never loosened


def test_operator_stored_budget_wins_over_a_different_declaration(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.50")
    enforcer = _enforcer(h)

    with pytest.raises(BudgetDeclarationError) as exc_info:
        _reserve(enforcer, declared_limit_usd=Decimal("100"))

    assert exc_info.value.reason == "conflict"


def test_declaration_above_operator_ceiling_is_refused_not_clamped(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    enforcer = _enforcer(h, per_work_max_usd=Decimal("1.00"))

    with pytest.raises(BudgetDeclarationError) as exc_info:
        _reserve(enforcer, declared_limit_usd=Decimal("1.01"))

    assert exc_info.value.reason == "above_max"
    assert h.budgets.list() == []


def test_declarations_can_be_turned_off(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    enforcer = _enforcer(h, allow_declared_budgets=False)

    with pytest.raises(BudgetDeclarationError) as exc_info:
        _reserve(enforcer, declared_limit_usd=Decimal("1.00"))

    assert exc_info.value.reason == "disabled"


def test_declaration_without_work_id_is_refused(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    enforcer = _enforcer(h)

    with pytest.raises(BudgetDeclarationError) as exc_info:
        _reserve(enforcer, attributes={}, declared_limit_usd=Decimal("1.00"))

    assert exc_info.value.reason == "missing_work_id"


def test_declaration_never_loosens_other_budgets(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.003", scope="global", window="daily")
    enforcer = _enforcer(h)

    _reserve(enforcer, declared_limit_usd=Decimal("100"))
    with pytest.raises(BudgetExceededError) as exc_info:
        _reserve(enforcer, declared_limit_usd=Decimal("100"))

    assert exc_info.value.budget_id == "global:_:daily"


def test_declared_budget_with_unpriced_model_is_refused(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    enforcer = _enforcer(h)

    with pytest.raises(BudgetUnpricedModelError):
        _reserve(enforcer, model="mystery-model", declared_limit_usd=Decimal("1.00"))


# ---------------------------------------------------------------------------
# Enforcer: inherited default
# ---------------------------------------------------------------------------


def test_default_per_work_budget_applies_to_any_new_work_id(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    enforcer = _enforcer(h, per_work_default_usd=Decimal("0.003"))

    assert _reserve(enforcer) is not None
    with pytest.raises(BudgetExceededError):
        _reserve(enforcer)
    [budget] = h.budgets.list()
    assert budget.limit_usd == Decimal("0.003")


def test_default_is_not_applied_without_a_work_id(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    enforcer = _enforcer(h, per_work_default_usd=Decimal("0.003"))

    assert _reserve(enforcer, attributes={}) is None
    assert h.budgets.list() == []


def test_declaration_takes_precedence_over_the_default_for_a_new_id(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    enforcer = _enforcer(h, per_work_default_usd=Decimal("0.003"))

    _reserve(enforcer, declared_limit_usd=Decimal("0.50"))

    [budget] = h.budgets.list()
    assert budget.limit_usd == Decimal("0.50")


def test_no_declaration_and_no_default_changes_nothing(tmp_path: Path) -> None:
    h = make_harness(tmp_path)

    assert _reserve(_enforcer(h)) is None
    assert h.budgets.list() == []


# ---------------------------------------------------------------------------
# Concurrency: first-seen ids (the unseen-id case)
# ---------------------------------------------------------------------------


async def test_concurrent_first_requests_for_an_unseen_id_cannot_bypass(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    enforcer = _enforcer(h)
    provider = GatedProvider()
    engine = h.openai_engine(provider)
    engine._budgets = enforcer  # the engine under test uses the declaring enforcer

    tasks = [
        asyncio.create_task(
            engine.execute(_chat(), attributes=ATTRS, declared_budget_usd=Decimal("0.005"))
        )
        for _ in range(6)
    ]
    await settle_tasks()
    provider.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert sum(not isinstance(r, BaseException) for r in results) == 2
    assert len(provider.calls) == 2
    assert len(h.budgets.list()) == 1


async def test_concurrent_first_requests_inheriting_the_default_cannot_bypass(
    tmp_path: Path,
) -> None:
    h = make_harness(tmp_path)
    provider = GatedProvider()
    engine = h.openai_engine(provider)
    engine._budgets = _enforcer(h, per_work_default_usd=Decimal("0.005"))

    tasks = [asyncio.create_task(engine.execute(_chat(), attributes=ATTRS)) for _ in range(6)]
    await settle_tasks()
    provider.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert sum(not isinstance(r, BaseException) for r in results) == 2
    assert len(provider.calls) == 2


async def test_refused_declaration_is_recorded_on_a_receipt(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    provider = FakeProvider()
    engine = h.openai_engine(provider)
    engine._budgets = _enforcer(h, per_work_max_usd=Decimal("1"))

    with pytest.raises(BudgetDeclarationError):
        await engine.execute(_chat(), attributes=ATTRS, declared_budget_usd=Decimal("2"))

    assert provider.calls == []
    [receipt] = h.receipts.read_all()[0]
    assert receipt.status == "error"


def test_budgets_persist_like_stored_ones(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    _reserve(_enforcer(h), declared_limit_usd=Decimal("0.005"))

    restarted = BudgetStore(h.budgets.db_path)

    assert [b.budget_id for b in restarted.list()] == [f"work_id:{WORK_ID}:per_work"]


# ---------------------------------------------------------------------------
# Through the HTTP gateway: header parsing, validation, never forwarded
# ---------------------------------------------------------------------------


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
def _no_gateway_token_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFERRAIL_GATEWAY_TOKEN", raising=False)


def _client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, upstream: _Upstream, **budgets: Any
) -> TestClient:
    config = InferrailConfig.model_validate(
        {
            "providers": {"openai": {"type": "openai", "api_key_env": "TEST_OPENAI_API_KEY"}},
            "routes": {"default": {"provider": "openai", "model": "gpt-4o-mini"}},
            "telemetry": {"sink": "none"},
            "receipts": {"sink": "sqlite", "path": str(tmp_path / "receipts.db")},
            "budgets": {"enabled": True, "path": str(tmp_path / "budgets.db"), **budgets},
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


def _body() -> dict[str, Any]:
    return {"model": "default", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


def test_header_declares_a_budget_and_is_never_forwarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = _Upstream()
    client = _client(monkeypatch, tmp_path, upstream)

    response = client.post(
        "/v1/chat/completions", json=_body(),
        headers={"X-Inferrail-Attribute-Work-Id": "run-9", "X-Inferrail-Budget-Usd": "0.50"},
    )

    assert response.status_code == 200, response.text
    [sent] = upstream.requests
    assert not any(k.lower().startswith("x-inferrail") for k in sent.headers)
    assert "0.50" not in json.dumps(json.loads(sent.content))
    [budget] = BudgetStore(tmp_path / "budgets.db").list()
    assert budget.budget_id == "work_id:run-9:per_work"
    assert budget.limit_usd == Decimal("0.50")


@pytest.mark.parametrize("value", ["abc", "0", "-1", "NaN", "Infinity", "1e400", ""])
def test_invalid_header_values_are_refused_before_the_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    upstream = _Upstream()
    client = _client(monkeypatch, tmp_path, upstream)

    response = client.post(
        "/v1/chat/completions", json=_body(),
        headers={"X-Inferrail-Attribute-Work-Id": "run-9", "X-Inferrail-Budget-Usd": value},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INFERRAIL_E013"
    assert upstream.requests == []


def test_header_without_work_id_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = _Upstream()
    client = _client(monkeypatch, tmp_path, upstream)

    response = client.post(
        "/v1/chat/completions", json=_body(), headers={"X-Inferrail-Budget-Usd": "0.50"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["details"]["reason"] == "missing_work_id"
    assert upstream.requests == []


def test_conflict_is_a_clear_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    upstream = _Upstream()
    client = _client(monkeypatch, tmp_path, upstream)
    h1 = {"X-Inferrail-Attribute-Work-Id": "run-9", "X-Inferrail-Budget-Usd": "0.50"}
    h2 = {"X-Inferrail-Attribute-Work-Id": "run-9", "X-Inferrail-Budget-Usd": "9.00"}

    assert client.post("/v1/chat/completions", json=_body(), headers=h1).status_code == 200
    response = client.post("/v1/chat/completions", json=_body(), headers=h2)

    assert response.status_code == 400
    assert response.json()["error"]["details"]["reason"] == "conflict"
    assert len(upstream.requests) == 1


def test_default_from_config_protects_runs_with_no_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = _Upstream()
    client = _client(monkeypatch, tmp_path, upstream, per_work_default_usd="0.0000001")

    response = client.post(
        "/v1/chat/completions", json=_body(), headers={"X-Inferrail-Attribute-Work-Id": "run-9"}
    )

    assert response.status_code == 402
    assert upstream.requests == []


def test_header_on_the_anthropic_route_declares_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = _Upstream()
    client = _client(monkeypatch, tmp_path, upstream)

    response = client.post(
        "/v1/messages",
        json={
            "model": "default", "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"X-Inferrail-Attribute-Work-Id": "run-a", "X-Inferrail-Budget-Usd": "abc"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INFERRAIL_E013"


def test_header_is_refused_not_ignored_when_budgets_are_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = _Upstream()
    config = InferrailConfig.model_validate(
        {
            "providers": {"openai": {"type": "openai", "api_key_env": "TEST_OPENAI_API_KEY"}},
            "routes": {"default": {"provider": "openai", "model": "gpt-4o-mini"}},
            "telemetry": {"sink": "none"},
            "receipts": {"sink": "sqlite", "path": str(tmp_path / "receipts.db")},
        }
    )
    provider = OpenAIProvider(
        name="openai", api_key="k", base_url="https://api.openai.com/v1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
        is_verified_openai=True,
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})
    client = TestClient(app_module.create_app(config))

    response = client.post(
        "/v1/chat/completions", json=_body(),
        headers={"X-Inferrail-Attribute-Work-Id": "run-9", "X-Inferrail-Budget-Usd": "0.50"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["details"]["reason"] == "disabled"
    assert upstream.requests == []
