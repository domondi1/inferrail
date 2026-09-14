"""End-to-end (through the real FastAPI app, via TestClient) tests for
the local control API — see docs/adr/0016-local-control-api.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from inferrail.budgets.schema import Budget, new_budget_id
from inferrail.budgets.store import BudgetStore
from inferrail.config.models import InferrailConfig
from inferrail.gateway import app as app_module
from inferrail.localapi import routes as localapi_routes
from inferrail.receipts.schema import InferenceReceipt
from inferrail.receipts.sqlite_store import ReceiptsStore
from inferrail.work.builder import append_outcome
from inferrail.work.schema import WorkOutcomeRecord


@pytest.fixture(autouse=True)
def _no_gateway_token_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFERRAIL_GATEWAY_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _fast_stream_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(localapi_routes, "STREAM_POLL_INTERVAL_SECONDS", 0.01)


def _app_mode_config(tmp_path: Path) -> InferrailConfig:
    return InferrailConfig.model_validate(
        {
            "providers": {
                "openai": {"type": "openai", "api_key_env": "TEST_OPENAI_API_KEY"},
            },
            "routes": {
                "default": {"provider": "openai", "model": "gpt-4o-mini"},
            },
            "telemetry": {"sink": "none"},
            "receipts": {"sink": "sqlite", "path": str(tmp_path / "receipts.db")},
            "budgets": {"enabled": True, "path": str(tmp_path / "budgets.db")},
        }
    )


def _make_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[TestClient, str, InferrailConfig]:
    config = _app_mode_config(tmp_path)
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})
    app = app_module.create_app(
        config, app_mode=True, local_outcomes_path=tmp_path / "work-outcomes.jsonl"
    )
    return TestClient(app), app.state.local_api_token, config


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _receipt(**overrides: Any) -> InferenceReceipt:
    defaults: dict[str, Any] = dict(
        receipt_id="ir_test",
        request_id="req_test",
        route="default",
        provider="openai",
        model="gpt-4o-mini",
        status="success",
        total_latency_ms=10.0,
    )
    defaults.update(overrides)
    return InferenceReceipt(**defaults)  # type: ignore[arg-type]


def test_every_local_route_requires_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _token, _config = _make_client(monkeypatch, tmp_path)

    assert client.get("/v1/local/receipts").status_code == 401
    assert client.get("/v1/local/work").status_code == 401
    assert client.get("/v1/local/work/w1").status_code == 401
    assert client.get("/v1/local/budgets").status_code == 401
    assert client.post("/v1/local/budgets", json={}).status_code == 401
    assert client.delete("/v1/local/budgets/x").status_code == 401


def test_wrong_token_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, _token, _config = _make_client(monkeypatch, tmp_path)

    response = client.get("/v1/local/receipts", headers=_auth("not-the-real-token"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INFERRAIL_E011"


def test_health_and_gateway_routes_still_work_without_the_local_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The local API's mandatory token must never leak into the (unrelated,
    # optional-token) gateway/inference routes.
    client, _token, _config = _make_client(monkeypatch, tmp_path)

    assert client.get("/health").status_code == 200


def test_list_receipts_paginates_and_filters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, token, config = _make_client(monkeypatch, tmp_path)
    store = ReceiptsStore(config.receipts.path)
    for i in range(3):
        store.emit(_receipt(receipt_id=f"ir_{i}", attributes={"project": "acme"}))
    store.emit(_receipt(receipt_id="ir_other", attributes={"project": "other"}))

    page = client.get(
        "/v1/local/receipts", params={"project": "acme", "limit": 2}, headers=_auth(token)
    ).json()

    assert page["total"] == 3
    assert page["limit"] == 2
    assert len(page["receipts"]) == 2
    assert all(r["attributes"]["project"] == "acme" for r in page["receipts"])


def test_list_work_and_get_one_work_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, token, config = _make_client(monkeypatch, tmp_path)
    store = ReceiptsStore(config.receipts.path)
    store.emit(_receipt(receipt_id="ir_1", attributes={"work_id": "wf_1"}))
    outcomes_path = tmp_path / "work-outcomes.jsonl"
    append_outcome(
        outcomes_path,
        WorkOutcomeRecord(
            work_id="wf_1", outcome_status="accepted", recorded_at="2026-01-01T00:00:00Z"
        ),
    )

    listing = client.get("/v1/local/work", headers=_auth(token)).json()
    single = client.get("/v1/local/work/wf_1", headers=_auth(token)).json()
    missing = client.get("/v1/local/work/does-not-exist", headers=_auth(token))

    assert len(listing) == 1
    assert listing[0]["work_id"] == "wf_1"
    assert single["outcome_status"] == "accepted"
    assert missing.status_code == 404


def test_budgets_crud(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, token, _config = _make_client(monkeypatch, tmp_path)

    created = client.post(
        "/v1/local/budgets",
        headers=_auth(token),
        json={"scope": "global", "window": "daily", "mode": "block", "limit_usd": "0.5"},
    )
    listed = client.get("/v1/local/budgets", headers=_auth(token)).json()
    deleted = client.delete("/v1/local/budgets/global:_:daily", headers=_auth(token))
    deleted_again = client.delete("/v1/local/budgets/global:_:daily", headers=_auth(token))

    assert created.status_code == 201
    assert created.json()["budget_id"] == "global:_:daily"
    assert len(listed) == 1
    assert deleted.status_code == 204
    assert deleted_again.status_code == 404


def test_create_budget_rejects_invalid_scope_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, token, _config = _make_client(monkeypatch, tmp_path)

    response = client.post(
        "/v1/local/budgets",
        headers=_auth(token),
        json={"scope": "project", "window": "monthly", "mode": "block", "limit_usd": "1"},
    )

    assert response.status_code == 400


def test_create_budget_via_api_is_visible_to_enforcement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The local API and the enforcer share the same BudgetStore file —
    # confirm a budget created through the API is the same one
    # BudgetStore.list() (what BudgetEnforcer reads) sees.
    client, token, config = _make_client(monkeypatch, tmp_path)

    client.post(
        "/v1/local/budgets",
        headers=_auth(token),
        json={"scope": "global", "window": "daily", "mode": "warn", "limit_usd": "1"},
    )

    budgets = BudgetStore(config.budgets.path).list()
    assert [b.budget_id for b in budgets] == ["global:_:daily"]


class _StubRequest:
    """A minimal stand-in for `starlette.Request` — only
    `is_disconnected()` is used by `_tail_new_receipts`. Disconnects
    after a fixed number of polls, and emits a new receipt into `store`
    partway through so the test can assert it (and only it) was yielded.
    """

    def __init__(self, store: ReceiptsStore, *, emit_after_poll: int, disconnect_after: int):
        self._store = store
        self._emit_after_poll = emit_after_poll
        self._disconnect_after = disconnect_after
        self.polls = 0

    async def is_disconnected(self) -> bool:
        self.polls += 1
        if self.polls == self._emit_after_poll:
            self._store.emit(_receipt(receipt_id="ir_after_stream_opened"))
        return self.polls > self._disconnect_after


async def test_stream_yields_only_receipts_emitted_after_it_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Exercises `_tail_new_receipts` directly rather than through
    # TestClient's SSE transport: a real, infinite-by-design polling
    # generator has no clean way to force a client "disconnect" through
    # that synchronous test harness, whereas the generator's own
    # `is_disconnected()` contract is trivial to stub.
    from inferrail.localapi.routes import _tail_new_receipts

    store = ReceiptsStore(tmp_path / "receipts.db")
    store.emit(_receipt(receipt_id="ir_before_stream_opened"))
    request = _StubRequest(store, emit_after_poll=2, disconnect_after=3)

    chunks = [chunk async for chunk in _tail_new_receipts(store, request)]

    text = b"".join(chunks).decode()
    assert "ir_after_stream_opened" in text
    assert "ir_before_stream_opened" not in text


def test_pre_seeded_budget_store_is_reused_not_recreated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _app_mode_config(tmp_path)
    BudgetStore(config.budgets.path).set(
        Budget(
            budget_id=new_budget_id("global", None, "daily"), scope="global",
            window="daily", mode="warn", limit_usd=10,
        )
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})
    app = app_module.create_app(config, app_mode=True)
    token = app.state.local_api_token
    client = TestClient(app)

    response = client.get("/v1/local/budgets", headers=_auth(token))

    assert [b["budget_id"] for b in response.json()] == ["global:_:daily"]
