"""Tests for hosted/budget_gateway (Inferrail Hosted): workspaces, governed-run metering, the
credit ledger, Stripe webhook verification, provider-key pass-through and the x402 credit rail.

Providers are `httpx.MockTransport` (no network, no key). The x402 rail uses the real x402 2.22.0
middleware with an offline fake facilitator (`_x402_fake_facilitator.py`).
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("x402")
pytest.importorskip("eth_account")
pytest.importorskip("fastapi")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _x402_fake_facilitator import (  # noqa: E402
    FakeFacilitator,
    decode_payment_required,
    settle_failure,
    sign_payment,
)

HOSTED = Path(__file__).resolve().parents[3] / "hosted" / "budget_gateway"
PROVIDER_KEY = "sk-test-provider-key-0123456789abcdef"
PAY_TO = "0x" + "9" * 40


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HOSTED / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mods():
    _load("workspaces", "workspaces.py")
    _load("stripe_checkout", "stripe_checkout.py")
    return _load("budget_gateway_service", "service.py")


@pytest.fixture
def ledger(tmp_path):
    ws = _load("workspaces", "workspaces.py")
    return ws.WorkspaceLedger(tmp_path / "w.sqlite3", free_runs_per_month=2)


# -- ledger ---------------------------------------------------------------------------------------


def test_free_allowance_then_refusal_and_repeat_runs_are_free(ledger):
    ws, _ = ledger.create_workspace()
    t = 1_791_000_000
    assert ledger.admit_run(ws, "a", t).admitted
    assert ledger.admit_run(ws, "b", t).admitted
    again = ledger.admit_run(ws, "a", t)
    assert again.admitted and not again.new_run
    refused = ledger.admit_run(ws, "c", t)
    assert not refused.admitted and refused.month_runs == 2


def test_allowance_resets_each_utc_month(ledger):
    ws, _ = ledger.create_workspace()
    for w in ("a", "b"):
        ledger.admit_run(ws, w, 1_790_000_000)  # 2026-09
    assert not ledger.admit_run(ws, "c", 1_790_000_000).admitted
    assert ledger.admit_run(ws, "c", 1_792_700_000).admitted  # 2026-10


def test_credits_are_spent_past_the_allowance_and_then_exhaust(ledger):
    ws, _ = ledger.create_workspace()
    t = 1_791_000_000
    ledger.admit_run(ws, "a", t)
    ledger.admit_run(ws, "b", t)
    assert ledger.grant("stripe", "cs_1", ws, 1, 100)
    paid = ledger.admit_run(ws, "c", t)
    assert paid.admitted and paid.paid and paid.credits_remaining == 0
    assert not ledger.admit_run(ws, "d", t).admitted


def test_a_payment_reference_grants_at_most_once(ledger):
    ws, _ = ledger.create_workspace()
    assert ledger.grant("stripe", "cs_1", ws, 10, 1000)
    assert not ledger.grant("stripe", "cs_1", ws, 10, 1000)
    assert ledger.usage(ws).credits_remaining == 10
    assert ledger.revenue() == {"stripe": 1000}


def test_concurrent_new_runs_never_spend_one_credit_twice(ledger):
    ws, _ = ledger.create_workspace()
    t = 1_791_000_000
    ledger.admit_run(ws, "a", t)
    ledger.admit_run(ws, "b", t)
    ledger.grant("x402", "r1", ws, 1, 100)
    results = []
    threads = [
        threading.Thread(
            target=lambda i=i: results.append(ledger.admit_run(ws, f"run-{i}", t).admitted)
        )
        for i in range(20)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results.count(True) == 1
    assert ledger.usage(ws, t).credits_remaining == 0


def test_pending_purchase_becomes_credits_only_on_settlement(ledger):
    ws, _ = ledger.create_workspace()
    ledger.record_pending("x402", "0xabc:0x1", ws, 1000, 100)
    assert ledger.usage(ws).credits_remaining == 0
    assert [p["ref"] for p in ledger.pending()] == ["0xabc:0x1"]
    assert ledger.settle_pending("x402", "0xabc:0x1")
    assert not ledger.settle_pending("x402", "0xabc:0x1")
    assert ledger.usage(ws).credits_remaining == 1000 and ledger.pending() == []
    ledger.record_pending("x402", "0xabc:0x2", ws, 1000, 100)
    ledger.fail_pending("x402", "0xabc:0x2")
    assert ledger.usage(ws).credits_remaining == 1000 and ledger.pending() == []


def test_workspace_keys_are_stored_only_as_hashes(tmp_path, ledger):
    _, key = ledger.create_workspace()
    assert ledger.authenticate(key) is not None
    assert ledger.authenticate(key + "x") is None
    blob = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    assert key.encode() not in blob


# -- stripe ---------------------------------------------------------------------------------------


def _sign(body: bytes, secret: str, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_stripe_signature_verification(mods):
    sc = sys.modules["stripe_checkout"]
    body = b'{"type":"x"}'
    assert sc.verify_event(body, _sign(body, "whsec_1"), "whsec_1") == {"type": "x"}
    stale = _sign(body, "whsec_1", ts=int(time.time()) - 3600)
    for header in (_sign(body, "other"), stale, "v1=x"):
        with pytest.raises(sc.SignatureError):
            sc.verify_event(body, header, "whsec_1")
    with pytest.raises(sc.SignatureError):
        sc.verify_event(b'{"type":"y"}', _sign(body, "whsec_1"), "whsec_1")


STRIPE_ENV = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": "whsec_1"}


def _client(mods, tmp_path, env=None, facilitator=None, **extra):
    settings = mods.Settings.from_env(
        {
            "BG_DATA_DIR": str(tmp_path / "data"),
            "BG_FREE_RUNS_PER_MONTH": "2",
            **(env or {}),
            **extra,
        }
    )
    return TestClient(mods.create_app(settings, facilitator=facilitator))


def _workspace(client):
    r = client.post("/v1/workspaces")
    assert r.status_code == 201
    return r.json()["api_key"]


def _event(workspace, packs=1, amount=1000, status="paid", sid="cs_test_1"):
    return json.dumps(
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": sid,
                    "mode": "payment",
                    "payment_status": status,
                    "amount_total": amount,
                    "metadata": {"workspace_id": workspace, "packs": str(packs)},
                }
            },
        }
    ).encode()


def test_stripe_webhook_grants_once_and_only_for_paid_matching_sessions(mods, tmp_path):
    c = _client(mods, tmp_path, STRIPE_ENV)
    key = _workspace(c)
    ws = c.get("/v1/workspace", headers={"Authorization": f"Bearer {key}"}).json()["workspace_id"]

    def post(body, header=None):
        return c.post(
            "/v1/stripe/webhook",
            content=body,
            headers={"Stripe-Signature": header or _sign(body, "whsec_1")},
        )

    assert post(_event(ws, status="unpaid")).json()["granted"] is False
    assert post(_event(ws, amount=1)).json()["granted"] is False
    assert post(_event(ws), header=_sign(_event(ws), "wrong")).status_code == 400
    assert post(_event(ws)).json()["granted"] is True
    assert post(_event(ws)).json()["granted"] is False  # replay
    usage = c.get("/v1/workspace", headers={"Authorization": f"Bearer {key}"}).json()
    assert usage["credits_remaining"] == 10_000


def test_card_rail_is_off_unless_configured(mods, tmp_path):
    c = _client(mods, tmp_path)
    key = _workspace(c)
    r = c.post("/v1/credits/checkout", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 501
    assert c.post("/v1/stripe/webhook", content=b"{}").status_code == 404
    assert "card" not in c.get("/pricing").json()["credits"]


# -- proxy ----------------------------------------------------------------------------------------


def _mock_openai(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-mock",
                "model": body["model"],
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _chat(c, key, work="run-1", budget=None, provider_key=PROVIDER_KEY):
    headers = {"Authorization": f"Bearer {key}", "X-Inferrail-Attribute-Work-Id": work}
    if provider_key:
        headers["X-Provider-Api-Key"] = provider_key
    if budget:
        headers["X-Inferrail-Budget-Usd"] = budget
    return c.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "gpt-4o-mini",
            "max_tokens": 20,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )


def test_provider_key_is_forwarded_but_never_stored_or_returned(mods, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(mods, "openai_client_factory", _mock_openai(calls))
    c = _client(mods, tmp_path)
    key = _workspace(c)
    r = _chat(c, key)
    assert r.status_code == 200, r.text
    assert calls[0].headers["authorization"] == f"Bearer {PROVIDER_KEY}"
    assert PROVIDER_KEY not in r.text
    blob = b"".join(p.read_bytes() for p in (tmp_path / "data").iterdir() if p.is_file())
    assert PROVIDER_KEY.encode() not in blob
    work = c.get("/v1/work/run-1", headers={"Authorization": f"Bearer {key}"}).json()
    assert work["calls"] == 1


def test_missing_provider_key_is_refused_without_metering(mods, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(mods, "openai_client_factory", _mock_openai(calls))
    c = _client(mods, tmp_path)
    key = _workspace(c)
    assert _chat(c, key, provider_key=None).status_code == 400
    assert (
        c.get("/v1/workspace", headers={"Authorization": f"Bearer {key}"}).json()["governed_runs"]
        == 0
    )
    assert calls == []


def test_unknown_workspace_key_is_rejected(mods, tmp_path):
    c = _client(mods, tmp_path)
    assert _chat(c, "irw_nope").status_code == 401


def test_exhausted_allowance_gets_402_with_purchase_options_before_the_provider(
    mods, tmp_path, monkeypatch
):
    calls: list = []
    monkeypatch.setattr(mods, "openai_client_factory", _mock_openai(calls))
    c = _client(mods, tmp_path, {"BG_X402_PAY_TO": PAY_TO}, facilitator=FakeFacilitator())
    key = _workspace(c)
    assert _chat(c, key, "a").status_code == 200
    assert _chat(c, key, "a").status_code == 200  # same run: no new charge
    assert _chat(c, key, "b").status_code == 200
    r = _chat(c, key, "c")
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["error"] == "allowance_exhausted" and "x402" in detail["purchase"]
    assert len(calls) == 3


def test_declared_run_budget_is_enforced_before_the_provider(mods, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(mods, "openai_client_factory", _mock_openai(calls))
    c = _client(mods, tmp_path)
    key = _workspace(c)
    r = _chat(c, key, "tiny", budget="0.000000001")
    assert r.status_code == 402, r.text
    assert r.json()["error"]["type"] == "BudgetExceededError"
    assert calls == []
    assert _chat(c, key, "bad", budget="-1").status_code == 400


# -- x402 credit rail -----------------------------------------------------------------------------


def _buy(c, key, facilitator_ok=True):
    unpaid = c.post("/v1/credits/x402", headers={"Authorization": f"Bearer {key}"})
    assert unpaid.status_code == 402
    signed = sign_payment(decode_payment_required(unpaid))
    return c.post(
        "/v1/credits/x402",
        headers={"Authorization": f"Bearer {key}", "PAYMENT-SIGNATURE": signed.header},
    )


def test_x402_purchase_grants_credits_after_settlement(mods, tmp_path):
    fac = FakeFacilitator()
    c = _client(mods, tmp_path, {"BG_X402_PAY_TO": PAY_TO}, facilitator=fac)
    key = _workspace(c)
    r = _buy(c, key)
    assert r.status_code == 200, r.text
    assert [x.method for x in fac.calls] == ["verify", "settle"]
    usage = c.get("/v1/workspace", headers={"Authorization": f"Bearer {key}"}).json()
    assert usage["credits_remaining"] == 1000


def test_x402_settlement_failure_grants_nothing(mods, tmp_path):
    fac = FakeFacilitator(settle_outcomes=[settle_failure()])
    c = _client(mods, tmp_path, {"BG_X402_PAY_TO": PAY_TO}, facilitator=fac)
    key = _workspace(c)
    assert _buy(c, key).status_code == 402
    usage = c.get("/v1/workspace", headers={"Authorization": f"Bearer {key}"}).json()
    assert usage["credits_remaining"] == 0


def test_x402_payment_with_an_invalid_workspace_is_never_settled(mods, tmp_path):
    fac = FakeFacilitator()
    c = _client(mods, tmp_path, {"BG_X402_PAY_TO": PAY_TO}, facilitator=fac)
    unpaid = c.post("/v1/credits/x402")
    signed = sign_payment(decode_payment_required(unpaid))
    r = c.post(
        "/v1/credits/x402",
        headers={"Authorization": "Bearer irw_bad", "PAYMENT-SIGNATURE": signed.header},
    )
    assert r.status_code == 401
    assert fac.calls_to("settle") == []


def test_mainnet_credit_sales_need_explicit_approval(mods, tmp_path):
    with pytest.raises(RuntimeError):
        mods.Settings.from_env({"BG_X402_NETWORK": "eip155:8453", "BG_X402_PAY_TO": PAY_TO})
    assert (
        mods.Settings.from_env(
            {
                "BG_X402_NETWORK": "eip155:8453",
                "BG_X402_PAY_TO": PAY_TO,
                "BG_X402_MAINNET_APPROVED": "1",
            }
        ).x402_pay_to
        == PAY_TO
    )


def test_pricing_and_llms_txt_publish_the_same_terms(mods, tmp_path):
    c = _client(mods, tmp_path, {"BG_X402_PAY_TO": PAY_TO}, facilitator=FakeFacilitator())
    p = c.get("/pricing").json()
    assert p["free_governed_runs_per_month"] == 2 and p["subscription"] is None
    assert p["credits"]["x402"]["price_usd"] == "1.00"
    txt = c.get("/llms.txt").text
    assert "X-Provider-Api-Key" in txt and "/pricing" in txt


def test_workspace_creation_is_throttled_per_address(mods, tmp_path):
    c = _client(mods, tmp_path, BG_CREATIONS_PER_IP_PER_HOUR="2")
    assert c.post("/v1/workspaces").status_code == 201
    assert c.post("/v1/workspaces").status_code == 201
    assert c.post("/v1/workspaces").status_code == 429
