"""Service-wiring tests for hosted/cost_gateway/service.py: trial
issuance, per-tenant isolation, key handling (never echoed/leaked,
cross-tenant isolation, expiry tightening), demo mode, budget
enforcement, and rate limiting.

Uses only dependencies already required by `inferrail` core (fastapi,
pydantic) -- no optional extra needed. Real-provider proxying
(`/v1/chat/completions`, `/v1/messages` with a genuine key) is not
exercised here -- it would require either real network access or
refactoring `service.py` to accept an injectable `httpx` client, neither
of which this pass does; the "no key configured" 400 path and the
budget/isolation/key-handling guarantees around it are fully covered
instead. Live-provider verification is a manual step, same "flag, don't
hide" pattern the AP release used for its own `OPENAI_API_KEY`-gated
tests.

Loads hosted/cost_gateway's flat-file modules by explicit path via
`importlib.util`, registering them under their own plain names ("auth",
"tenant_store", "trial", "keys", "demo_provider") -- the same pattern
`test_ap_exceptions_service.py` already established, and the same
caveat applies: `hosted/ap_exceptions` has its own same-named
"auth"/"tenant_store" modules. Because both test files' fixtures are
function-scoped and reload their own versions into `sys.modules`
immediately before constructing their own app, within any single test
function the names always resolve to *this* directory's modules
regardless of what any other test file did earlier in the same pytest
session -- see that module's docstring for the fuller explanation, which
applies here unchanged.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

HOSTED_DIR = Path(__file__).resolve().parents[3] / "hosted" / "cost_gateway"


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HOSTED_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def service_module(monkeypatch, tmp_path):
    monkeypatch.setenv("COST_GATEWAY_RATE_LIMIT_MAX_REQUESTS", "1000")
    monkeypatch.setenv("COST_GATEWAY_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("COST_GATEWAY_ISSUE_MAX_PER_IP", "1000")
    monkeypatch.setenv("COST_GATEWAY_DEMO_TTL_SECONDS", "3600")
    monkeypatch.setenv("COST_GATEWAY_REAL_KEY_TTL_SECONDS", "60")
    monkeypatch.setenv("COST_GATEWAY_DAILY_BUDGET_USD", "1.00")
    _load("trial", "trial.py")
    _load("keys", "keys.py")
    _load("tenant_store", "tenant_store.py")
    _load("auth", "auth.py")
    _load("demo_provider", "demo_provider.py")
    return _load("cost_gateway_service_under_test", "service.py")


@pytest.fixture
def app(service_module, tmp_path):
    return service_module.create_app(tmp_path / "data")


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def _issue(client) -> dict:
    resp = client.post("/v1/trial")
    assert resp.status_code == 200
    return resp.json()


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _demo_payload() -> dict:
    return {"model": "anything", "messages": [{"role": "user", "content": "hello"}]}


# --- health / issuance -------------------------------------------------


def test_health_requires_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_trial_returns_api_key_and_defaults_to_demo_mode(client):
    trial = _issue(client)
    assert trial["api_key"].startswith("trial_")
    assert trial["mode"] == "demo"
    assert trial["openai_configured"] is False
    assert trial["anthropic_configured"] is False
    assert trial["seconds_remaining"] > 0
    assert trial["dashboard_url"] is None


# --- auth ----------------------------------------------------------------


def test_missing_auth_header_is_401(client):
    resp = client.get("/v1/receipts")
    assert resp.status_code == 401


def test_invalid_api_key_is_401(client):
    resp = client.get("/v1/receipts", headers=_auth("not-a-real-key"))
    assert resp.status_code == 401


def test_expired_trial_is_401(service_module, tmp_path, monkeypatch):
    monkeypatch.setenv("COST_GATEWAY_DEMO_TTL_SECONDS", "0.05")
    app = service_module.create_app(tmp_path / "data")
    from fastapi.testclient import TestClient

    client = TestClient(app)
    trial = _issue(client)
    time.sleep(0.2)
    resp = client.get(f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"]))
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"]


def test_tenant_id_mismatch_in_path_is_403(client):
    trial_a = _issue(client)
    trial_b = _issue(client)
    resp = client.get(
        f"/v1/trial/{trial_b['tenant_id']}", headers=_auth(trial_a["api_key"])
    )
    assert resp.status_code == 403


# --- demo mode -------------------------------------------------------------


def test_demo_chat_completion_works_without_any_key(client):
    trial = _issue(client)
    resp = client.post(
        "/v1/demo/chat/completions", json=_demo_payload(), headers=_auth(trial["api_key"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"]
    assert body["usage"]["prompt_tokens"] is not None


def test_demo_receipt_appears_in_receipts_listing(client):
    trial = _issue(client)
    client.post(
        "/v1/demo/chat/completions",
        json=_demo_payload(),
        headers={**_auth(trial["api_key"]), "X-Inferrail-Attribute-Customer": "acme"},
    )
    resp = client.get("/v1/receipts", headers=_auth(trial["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    receipt = body["receipts"][0]
    assert receipt["provider"] == "demo"
    assert receipt["status"] == "success"
    assert receipt["estimated_cost_usd"] is not None
    assert receipt["attributes"] == {"customer": "acme"}
    # Structural payload-free guarantee: no field anywhere resembling
    # prompt/response content.
    assert "content" not in receipt
    assert "message" not in receipt
    assert "prompt" not in receipt


# --- real-key gating (no network exercised) ---------------------------------


def test_real_chat_completions_requires_key_returns_400(client):
    trial = _issue(client)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(trial["api_key"]),
    )
    assert resp.status_code == 400
    assert "no OpenAI key configured" in resp.json()["detail"]


def test_real_messages_requires_key_returns_400(client):
    trial = _issue(client)
    resp = client.post(
        "/v1/messages",
        json={
            "model": "claude",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=_auth(trial["api_key"]),
    )
    assert resp.status_code == 400
    assert "no Anthropic key configured" in resp.json()["detail"]


# --- key submission, validation, and leakage ------------------------------


def test_submit_keys_requires_at_least_one(client):
    trial = _issue(client)
    resp = client.post(
        f"/v1/trial/{trial['tenant_id']}/keys", json={}, headers=_auth(trial["api_key"])
    )
    assert resp.status_code == 422


def test_submit_keys_rejects_whitespace_key_without_echoing_value(client):
    trial = _issue(client)
    secret = "sk-has a space in it"
    resp = client.post(
        f"/v1/trial/{trial['tenant_id']}/keys",
        json={"openai_key": secret},
        headers=_auth(trial["api_key"]),
    )
    assert resp.status_code == 422
    assert secret not in resp.text


def test_submit_keys_marks_openai_configured_and_tightens_expiry(client):
    trial = _issue(client)
    before_remaining = trial["seconds_remaining"]
    resp = client.post(
        f"/v1/trial/{trial['tenant_id']}/keys",
        json={"openai_key": "sk-fake-key-for-tests"},
        headers=_auth(trial["api_key"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["openai_configured"] is True
    assert body["anthropic_configured"] is False
    assert body["mode"] == "real_key"
    # COST_GATEWAY_REAL_KEY_TTL_SECONDS=60 in the fixture, well under the
    # 3600s demo TTL -- expiry must have tightened, never extended.
    assert body["seconds_remaining"] <= 60
    assert body["seconds_remaining"] < before_remaining


def test_keys_never_appear_in_any_response_body(client):
    trial = _issue(client)
    secret = "sk-supersecretmarkervalue12345"
    submit_resp = client.post(
        f"/v1/trial/{trial['tenant_id']}/keys",
        json={"openai_key": secret},
        headers=_auth(trial["api_key"]),
    )
    assert secret not in submit_resp.text
    status_resp = client.get(
        f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"])
    )
    assert secret not in status_resp.text
    bad_call_resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(trial["api_key"]),
    )
    assert secret not in bad_call_resp.text


def test_cross_tenant_key_isolation(client):
    trial_a = _issue(client)
    trial_b = _issue(client)
    client.post(
        f"/v1/trial/{trial_a['tenant_id']}/keys",
        json={"openai_key": "sk-tenant-a-only"},
        headers=_auth(trial_a["api_key"]),
    )
    status_b = client.get(
        f"/v1/trial/{trial_b['tenant_id']}", headers=_auth(trial_b["api_key"])
    ).json()
    assert status_b["openai_configured"] is False
    assert status_b["mode"] == "demo"
    # Tenant B cannot even ask about tenant A's status.
    cross = client.get(
        f"/v1/trial/{trial_a['tenant_id']}", headers=_auth(trial_b["api_key"])
    )
    assert cross.status_code == 403


def test_cross_tenant_receipts_isolation(client):
    trial_a = _issue(client)
    trial_b = _issue(client)
    client.post(
        "/v1/demo/chat/completions", json=_demo_payload(), headers=_auth(trial_a["api_key"])
    )
    receipts_b = client.get("/v1/receipts", headers=_auth(trial_b["api_key"])).json()
    assert receipts_b["total"] == 0
    receipts_a = client.get("/v1/receipts", headers=_auth(trial_a["api_key"])).json()
    assert receipts_a["total"] == 1


# --- deletion / teardown ----------------------------------------------------


def test_delete_keys_forgets_key(client):
    trial = _issue(client)
    client.post(
        f"/v1/trial/{trial['tenant_id']}/keys",
        json={"openai_key": "sk-to-be-forgotten"},
        headers=_auth(trial["api_key"]),
    )
    del_resp = client.delete(
        f"/v1/trial/{trial['tenant_id']}/keys", headers=_auth(trial["api_key"])
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["keys_removed"] is True
    status = client.get(
        f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"])
    ).json()
    assert status["openai_configured"] is False


def test_end_trial_removes_tenant(client):
    trial = _issue(client)
    resp = client.delete(f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"]))
    assert resp.status_code == 200
    assert resp.json()["ended"] is True
    # The API key is now dead -- any further authenticated call is 401.
    follow_up = client.get(
        f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"])
    )
    assert follow_up.status_code == 401


# --- rate limiting / budget --------------------------------------------------


def test_rate_limit_enforced(service_module, tmp_path, monkeypatch):
    monkeypatch.setenv("COST_GATEWAY_RATE_LIMIT_MAX_REQUESTS", "2")
    monkeypatch.setenv("COST_GATEWAY_RATE_LIMIT_WINDOW_SECONDS", "60")
    app = service_module.create_app(tmp_path / "data")
    from fastapi.testclient import TestClient

    client = TestClient(app)
    trial = _issue(client)
    # Issuance itself doesn't count against the per-tenant limiter (it's
    # unauthenticated) -- two authenticated calls should be the first two
    # to actually consume the budget.
    r1 = client.get(f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"]))
    r2 = client.get(f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"]))
    r3 = client.get(f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"]))
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429


def test_daily_budget_blocks_demo_requests_when_exceeded(
    service_module, tmp_path, monkeypatch
):
    monkeypatch.setenv("COST_GATEWAY_DAILY_BUDGET_USD", "0.0000001")
    app = service_module.create_app(tmp_path / "data")
    from fastapi.testclient import TestClient

    client = TestClient(app)
    trial = _issue(client)
    resp = client.post(
        "/v1/demo/chat/completions", json=_demo_payload(), headers=_auth(trial["api_key"])
    )
    assert resp.status_code == 402
    assert resp.json()["error"]["code"]
