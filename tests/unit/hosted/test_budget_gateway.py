"""Tests for hosted/budget_gateway (Inferrail Hosted).

Covered:
- governed-run metering and billing-on-answer;
- the credit ledger's financial states;
- Stripe verification, replay, interruption and refunds;
- x402 settlement, failure, crash-before-grant and on-chain reconciliation;
- provider-key handling;
- concurrency;
- workspace isolation;
- restart recovery;
- the single-instance constraint;
- the complete customer journey.

Providers are `httpx.MockTransport` (no network, no key). The x402 rail uses the real x402 2.22.0
middleware with an offline fake facilitator (`_x402_fake_facilitator.py`).
"""

from __future__ import annotations

import asyncio
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
T = 1_791_000_000  # 2026-10


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
    _load("chain_check", "chain_check.py")
    return _load("budget_gateway_service", "service.py")


@pytest.fixture
def ledger(tmp_path):
    ws = _load("workspaces", "workspaces.py")
    return ws.WorkspaceLedger(tmp_path / "w.sqlite3", free_runs_per_month=2)


def _bill(ledger, ws, work, t=T):
    a = ledger.hold_run(ws, work, t)
    if a.admitted:
        ledger.finish_call(ws, a.month, work, answered=True)
    return a


# == ledger: metering =============================================================================


def test_free_allowance_then_refusal_and_repeat_calls_in_a_run_are_free(ledger):
    ws, _ = ledger.create_workspace()
    assert _bill(ledger, ws, "a").admitted
    assert _bill(ledger, ws, "b").admitted
    assert _bill(ledger, ws, "a").admitted  # same run again
    refused = ledger.hold_run(ws, "c", T)
    assert not refused.admitted and refused.month_runs == 2
    assert ledger.usage(ws, T).billed_runs == 2


def test_allowance_resets_each_utc_month(ledger):
    ws, _ = ledger.create_workspace()
    for w in ("a", "b"):
        _bill(ledger, ws, w, 1_790_000_000)  # 2026-09
    assert not ledger.hold_run(ws, "c", 1_790_000_000).admitted
    assert ledger.hold_run(ws, "c", 1_792_700_000).admitted  # 2026-10


def test_a_run_whose_calls_are_never_answered_costs_nothing(ledger):
    ws, _ = ledger.create_workspace()
    ledger.grant("stripe", "cs_1", ws, 1, 100)
    _bill(ledger, ws, "a")
    _bill(ledger, ws, "b")
    held = ledger.hold_run(ws, "c", T)  # takes the last credit
    assert held.admitted and held.paid and held.credits_remaining == 0
    ledger.finish_call(ws, held.month, "c", answered=False)  # refused or provider error
    assert ledger.usage(ws, T).credits_remaining == 1  # credit returned
    assert ledger.usage(ws, T).month_runs == 2


def test_a_hold_is_kept_while_another_call_of_the_run_is_in_flight(ledger):
    ws, _ = ledger.create_workspace()
    a1 = ledger.hold_run(ws, "r", T)
    ledger.hold_run(ws, "r", T)  # second concurrent call in the same run
    ledger.finish_call(ws, a1.month, "r", answered=False)  # first fails
    assert ledger.usage(ws, T).month_runs == 1  # still held by the second
    ledger.finish_call(ws, a1.month, "r", answered=True)
    assert ledger.usage(ws, T).billed_runs == 1


def test_concurrent_new_runs_never_spend_one_credit_twice(ledger):
    ws, _ = ledger.create_workspace()
    _bill(ledger, ws, "a")
    _bill(ledger, ws, "b")
    ledger.grant("x402", "r1", ws, 1, 100)
    results: list[bool] = []
    threads = [
        threading.Thread(
            target=lambda i=i: results.append(ledger.hold_run(ws, f"run-{i}", T).admitted)
        )
        for i in range(25)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results.count(True) == 1
    assert ledger.usage(ws, T).credits_remaining == 0


def test_restart_releases_orphaned_holds_but_keeps_billed_runs(tmp_path, ledger):
    ws, _ = ledger.create_workspace()
    _bill(ledger, ws, "billed")
    ledger.hold_run(ws, "in-flight-at-crash", T)
    reopened = type(ledger)(tmp_path / "w.sqlite3", free_runs_per_month=2)
    assert reopened.release_orphaned_holds() == 1
    u = reopened.usage(ws, T)
    assert u.month_runs == 1 and u.billed_runs == 1


def test_workspace_keys_are_stored_only_as_hashes(tmp_path, ledger):
    _, key = ledger.create_workspace()
    assert ledger.authenticate(key) is not None
    assert ledger.authenticate(key + "x") is None
    assert ledger.authenticate("not-a-key") is None
    blob = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    assert key.encode() not in blob


# == ledger: money ================================================================================


def test_a_payment_reference_grants_at_most_once(ledger):
    ws, _ = ledger.create_workspace()
    assert ledger.grant("stripe", "cs_1", ws, 10, 1000, external_id="pi_1")
    assert not ledger.grant("stripe", "cs_1", ws, 10, 1000, external_id="pi_1")
    assert ledger.usage(ws).credits_remaining == 10
    assert ledger.revenue() == {"stripe": 1000}


def test_pending_purchase_becomes_credits_only_on_settlement(ledger):
    ws, _ = ledger.create_workspace()
    kw = {"authorizer": "0xabc", "nonce": "0x1", "valid_before": int(time.time()) + 600}
    ledger.record_pending("x402", "0xabc:0x1", ws, 1000, 100, **kw)
    assert ledger.usage(ws).credits_remaining == 0
    assert ledger.settle_pending("x402", "0xabc:0x1")
    assert not ledger.settle_pending("x402", "0xabc:0x1")
    assert ledger.usage(ws).credits_remaining == 1000 and ledger.pending() == []
    ledger.fail_pending("x402", "0xabc:0x1")  # a late failure never undoes a settlement
    assert ledger.usage(ws).credits_remaining == 1000


def test_reconciliation_resolves_pending_purchases_from_chain_truth(ledger):
    ws, _ = ledger.create_workspace()
    now = time.time()
    ledger.record_pending(
        "x402", "p:used", ws, 1000, 100, authorizer="0xa", nonce="0x1", valid_before=int(now) + 600
    )
    ledger.record_pending(
        "x402",
        "p:expired",
        ws,
        1000,
        100,
        authorizer="0xa",
        nonce="0x2",
        valid_before=int(now) - 1000,
    )
    ledger.record_pending(
        "x402", "p:live", ws, 1000, 100, authorizer="0xa", nonce="0x3", valid_before=int(now) + 600
    )
    ledger.record_pending(
        "x402",
        "p:rpc-down",
        ws,
        1000,
        100,
        authorizer="0xa",
        nonce="0x4",
        valid_before=int(now) - 1000,
    )
    chain = {"0x1": True, "0x2": False, "0x3": False, "0x4": None}
    out = ledger.reconcile_pending(lambda _a, n: chain[n], now=now)
    assert out == {"settled": 1, "failed": 1, "unresolved": 2}
    assert ledger.usage(ws).credits_remaining == 1000
    assert {p["ref"] for p in ledger.pending()} == {"p:live", "p:rpc-down"}
    again = ledger.reconcile_pending(lambda _a, n: chain[n], now=now)
    assert again["settled"] == 0 and ledger.usage(ws).credits_remaining == 1000


def test_refunds_reverse_credits_monotonically_and_block_negative_balances(ledger):
    ws, _ = ledger.create_workspace()
    ledger.grant("stripe", "cs_1", ws, 10, 1000, external_id="pi_1")
    assert ledger.apply_refund("stripe", None, 500, external_id="pi_1") == 5
    assert ledger.apply_refund("stripe", None, 500, external_id="pi_1") == 5  # replay
    assert ledger.apply_refund("stripe", None, 300, external_id="pi_1") == 5  # out of order
    assert ledger.usage(ws).credits_remaining == 5
    for w in ("a", "b", "c", "d", "e", "f", "g"):  # 2 free + 5 paid
        assert _bill(ledger, ws, w).admitted
    assert ledger.apply_refund("stripe", None, 1000, external_id="pi_1") == 10  # full refund
    assert ledger.usage(ws).credits_remaining == -5
    assert not ledger.hold_run(ws, "h", T).admitted  # no spending a negative balance
    assert ledger.apply_refund("stripe", None, 100, external_id="pi_unknown") is None
    assert ledger.revenue() == {"stripe": 0}


# == stripe =======================================================================================


def _sign(body: bytes, secret: str, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_stripe_signature_verification(mods):
    sc = sys.modules["stripe_checkout"]
    body = b'{"type":"x"}'
    assert sc.verify_event(body, _sign(body, "whsec_1"), "whsec_1") == {"type": "x"}
    stale = _sign(body, "whsec_1", ts=int(time.time()) - 3600)
    for header in (_sign(body, "other"), stale, "v1=x", ""):
        with pytest.raises(sc.SignatureError):
            sc.verify_event(body, header, "whsec_1")
    with pytest.raises(sc.SignatureError):
        sc.verify_event(b'{"type":"y"}', _sign(body, "whsec_1"), "whsec_1")


STRIPE_ENV = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": "whsec_1"}


def _settings(mods, tmp_path, env=None, **extra):
    return mods.Settings.from_env(
        {
            "BG_DATA_DIR": str(tmp_path / "data"),
            "BG_FREE_RUNS_PER_MONTH": "2",
            **(env or {}),
            **extra,
        }
    )


def _client(mods, tmp_path, env=None, facilitator=None, **extra):
    return TestClient(
        mods.create_app(_settings(mods, tmp_path, env, **extra), facilitator=facilitator),
        raise_server_exceptions=False,
    )


def _workspace(client):
    r = client.post("/v1/workspaces")
    assert r.status_code == 201
    return r.json()["api_key"]


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _ws_id(c, key):
    return c.get("/v1/workspace", headers=_auth(key)).json()["workspace_id"]


def _session_event(workspace, packs=1, amount=1000, status="paid", sid="cs_test_1"):
    return json.dumps(
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": sid,
                    "mode": "payment",
                    "payment_status": status,
                    "amount_total": amount,
                    "payment_intent": "pi_test_1",
                    "metadata": {"workspace_id": workspace, "packs": str(packs)},
                }
            },
        }
    ).encode()


def _refund_event(refunded):
    return json.dumps(
        {
            "type": "charge.refunded",
            "data": {"object": {"payment_intent": "pi_test_1", "amount_refunded": refunded}},
        }
    ).encode()


def _post_event(c, body, header=None):
    return c.post(
        "/v1/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": header or _sign(body, "whsec_1")},
    )


def test_stripe_webhook_grants_once_and_only_for_paid_matching_sessions(mods, tmp_path):
    c = _client(mods, tmp_path, STRIPE_ENV)
    key = _workspace(c)
    ws = _ws_id(c, key)
    assert _post_event(c, _session_event(ws, status="unpaid")).json()["granted"] is False
    assert _post_event(c, _session_event(ws, amount=1)).json()["granted"] is False
    bad = _session_event(ws)
    assert _post_event(c, bad, header=_sign(bad, "wrong")).status_code == 400
    assert _post_event(c, _session_event(ws)).json()["granted"] is True
    assert _post_event(c, _session_event(ws)).json()["granted"] is False  # replay
    assert c.get("/v1/workspace", headers=_auth(key)).json()["credits_remaining"] == 10_000


def test_checkout_redirect_alone_grants_nothing(mods, tmp_path):
    c = _client(mods, tmp_path, STRIPE_ENV)
    key = _workspace(c)
    assert c.get("/v1/credits/thanks").status_code == 200
    assert c.get("/v1/workspace", headers=_auth(key)).json()["credits_remaining"] == 0


def test_interrupted_webhook_processing_grants_exactly_once_on_retry(mods, tmp_path, monkeypatch):
    app = mods.create_app(_settings(mods, tmp_path, STRIPE_ENV))
    c = TestClient(app, raise_server_exceptions=False)
    key = _workspace(c)
    ws = _ws_id(c, key)
    real_grant = app.state.ledger.grant
    calls = {"n": 0}

    def crash_once(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("process died mid-webhook")
        return real_grant(*a, **kw)

    monkeypatch.setattr(app.state.ledger, "grant", crash_once)
    assert _post_event(c, _session_event(ws)).status_code == 500  # Stripe will retry
    assert c.get("/v1/workspace", headers=_auth(key)).json()["credits_remaining"] == 0
    assert _post_event(c, _session_event(ws)).json()["granted"] is True
    assert _post_event(c, _session_event(ws)).json()["granted"] is False
    assert c.get("/v1/workspace", headers=_auth(key)).json()["credits_remaining"] == 10_000


def test_stripe_refund_event_reverses_credits_idempotently(mods, tmp_path):
    c = _client(mods, tmp_path, STRIPE_ENV)
    key = _workspace(c)
    ws = _ws_id(c, key)
    _post_event(c, _session_event(ws))
    assert _post_event(c, _refund_event(1000)).json() == {
        "refund_applied": True,
        "runs_reversed": 10_000,
    }
    assert _post_event(c, _refund_event(1000)).json()["runs_reversed"] == 10_000
    statement = c.get("/v1/workspace/ledger", headers=_auth(key)).json()
    assert statement["credits_remaining"] == 0
    assert statement["reversals"][0]["runs"] == 10_000


def test_card_rail_is_off_unless_configured(mods, tmp_path):
    c = _client(mods, tmp_path)
    key = _workspace(c)
    assert c.post("/v1/credits/checkout", headers=_auth(key)).status_code == 501
    assert c.post("/v1/stripe/webhook", content=b"{}").status_code == 404
    assert "card" not in c.get("/pricing").json()["credits"]


def test_checkout_creates_a_session_with_our_metadata(mods, tmp_path, monkeypatch):
    seen = {}

    def handler(request):
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"url": "https://checkout.stripe.test/s/1"})

    monkeypatch.setattr(
        mods,
        "stripe_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    c = _client(mods, tmp_path, STRIPE_ENV)
    key = _workspace(c)
    r = c.post("/v1/credits/checkout?packs=3", headers=_auth(key))
    assert r.json()["checkout_url"] == "https://checkout.stripe.test/s/1"
    assert seen["form"]["metadata[packs]"] == "3"
    assert seen["form"]["metadata[workspace_id]"] == _ws_id(c, key)
    assert seen["form"]["line_items[0][price_data][unit_amount]"] == "1000"
    assert c.post("/v1/credits/checkout?packs=0", headers=_auth(key)).status_code == 400


# == proxy ========================================================================================


def _mock_openai(calls: list, status=200, delay=0.0, body_extra="", completion_tokens=4):
    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if delay:
            await asyncio.sleep(delay)
        if status != 200:
            return httpx.Response(
                status,
                json={
                    "error": {
                        "message": f"Incorrect API key provided: {PROVIDER_KEY}{body_extra}",
                        "type": "invalid_request_error",
                    }
                },
            )
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-mock",
                "model": body["model"],
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": completion_tokens},
            },
        )

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _headers(key, work="run-1", budget=None, provider_key=PROVIDER_KEY):
    h = {"Authorization": f"Bearer {key}", "X-Inferrail-Attribute-Work-Id": work}
    if provider_key:
        h["X-Provider-Api-Key"] = provider_key
    if budget:
        h["X-Inferrail-Budget-Usd"] = budget
    return h


CHAT = {"model": "gpt-4o-mini", "max_tokens": 20, "messages": [{"role": "user", "content": "hi"}]}


def _chat(c, key, work="run-1", budget=None, provider_key=PROVIDER_KEY, payload=CHAT):
    return c.post(
        "/v1/chat/completions", headers=_headers(key, work, budget, provider_key), json=payload
    )


def _all_bytes(directory: Path) -> bytes:
    return b"".join(p.read_bytes() for p in directory.iterdir() if p.is_file())


def test_provider_key_is_forwarded_but_never_stored_or_returned(mods, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(mods, "openai_client_factory", _mock_openai(calls))
    c = _client(mods, tmp_path)
    key = _workspace(c)
    r = _chat(c, key)
    assert r.status_code == 200, r.text
    assert calls[0].headers["authorization"] == f"Bearer {PROVIDER_KEY}"
    assert PROVIDER_KEY not in r.text
    assert PROVIDER_KEY.encode() not in _all_bytes(tmp_path / "data")
    work = c.get("/v1/work/run-1", headers=_auth(key)).json()
    assert work["calls"] == 1 and PROVIDER_KEY not in json.dumps(work)


def test_provider_errors_never_echo_the_key_and_are_not_billed(mods, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(mods, "openai_client_factory", _mock_openai(calls, status=401))
    c = _client(mods, tmp_path)
    key = _workspace(c)
    r = _chat(c, key)
    assert r.status_code >= 400
    assert PROVIDER_KEY not in r.text
    assert PROVIDER_KEY.encode() not in _all_bytes(tmp_path / "data")
    u = c.get("/v1/workspace", headers=_auth(key)).json()
    assert u["governed_runs"] == 0 and u["billed_runs"] == 0


def test_missing_provider_key_is_refused_without_metering(mods, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(mods, "openai_client_factory", _mock_openai(calls))
    c = _client(mods, tmp_path)
    key = _workspace(c)
    assert _chat(c, key, provider_key=None).status_code == 400
    assert c.get("/v1/workspace", headers=_auth(key)).json()["governed_runs"] == 0
    assert calls == []


def test_unknown_workspace_key_is_rejected(mods, tmp_path):
    c = _client(mods, tmp_path)
    assert _chat(c, "irw_nope").status_code == 401
    assert c.get("/v1/workspace").status_code == 401


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


def test_a_run_refused_by_its_own_budget_is_not_billed(mods, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(mods, "openai_client_factory", _mock_openai(calls))
    c = _client(mods, tmp_path)
    key = _workspace(c)
    r = _chat(c, key, "tiny", budget="0.000000001")
    assert r.status_code == 402, r.text
    assert r.json()["error"]["type"] == "BudgetExceededError"
    assert calls == []
    assert _chat(c, key, "bad", budget="-1").status_code == 400
    u = c.get("/v1/workspace", headers=_auth(key)).json()
    assert u["governed_runs"] == 0 and u["free_runs_remaining"] == 2


def test_concurrent_calls_in_one_run_cannot_overspend_its_budget(mods, tmp_path, monkeypatch):
    """Many clients, one run, one budget: the reservation admits only what fits."""
    calls: list = []
    monkeypatch.setattr(mods, "openai_client_factory", _mock_openai(calls, delay=0.2))
    app = mods.create_app(_settings(mods, tmp_path))
    key = TestClient(app).post("/v1/workspaces").json()["api_key"]
    payload = {**CHAT, "max_tokens": 1000}

    async def burst():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await asyncio.gather(
                *[
                    client.post(
                        "/v1/chat/completions",
                        json=payload,
                        headers=_headers(key, "shared-run", budget="0.002"),
                    )
                    for _ in range(12)
                ]
            )

    responses = asyncio.run(burst())
    ok = [r for r in responses if r.status_code == 200]
    refused = [r for r in responses if r.status_code == 402]
    assert 1 <= len(ok) == len(calls) <= 3  # gpt-4o-mini, 1,000 max tokens ≈ $0.0006 each
    assert len(ok) + len(refused) == 12
    usage = TestClient(app).get("/v1/workspace", headers=_auth(key)).json()
    assert usage["billed_runs"] == 1  # one run, however many calls


def test_budget_state_survives_a_restart(mods, tmp_path, monkeypatch):
    calls: list = []
    # 1,000 completion tokens of gpt-4o-mini ~= $0.0006: a second such call can't fit in $0.0011.
    monkeypatch.setattr(mods, "openai_client_factory", _mock_openai(calls, completion_tokens=1000))
    settings = _settings(mods, tmp_path)
    app = mods.create_app(settings)
    c = TestClient(app)
    key = _workspace(c)
    payload = {**CHAT, "max_tokens": 1000}
    first = c.post("/v1/chat/completions", json=payload, headers=_headers(key, "r", "0.0011"))
    assert first.status_code == 200
    app.state.release_instance_lock()  # the process exits
    c2 = TestClient(mods.create_app(settings))
    again = c2.post("/v1/chat/completions", json=payload, headers=_headers(key, "r", "0.0011"))
    assert again.status_code == 402  # the run's spend was kept across the restart
    assert len(calls) == 1
    assert c2.get("/v1/workspace", headers=_auth(key)).json()["billed_runs"] == 1


def test_a_second_instance_on_the_same_data_directory_refuses_to_start(mods, tmp_path):
    settings = _settings(mods, tmp_path)
    mods.create_app(settings)
    with pytest.raises(mods.InstanceLockError):
        mods.create_app(settings)


def test_workspaces_are_isolated(mods, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(mods, "openai_client_factory", _mock_openai(calls))
    c = _client(mods, tmp_path)
    a, b = _workspace(c), _workspace(c)
    payload = {**CHAT, "max_tokens": 1000}
    r_a = c.post("/v1/chat/completions", json=payload, headers=_headers(a, "same-id", "0.0011"))
    assert r_a.status_code == 200
    # Same run id in another workspace has its own, untouched budget.
    r_b = c.post("/v1/chat/completions", json=payload, headers=_headers(b, "same-id", "0.0011"))
    assert r_b.status_code == 200
    assert c.get("/v1/work/same-id", headers=_auth(a)).json()["calls"] == 1
    assert c.get("/v1/work/same-id", headers=_auth(b)).json()["calls"] == 1
    c.post("/v1/stripe/webhook")  # unconfigured: 404, nothing granted anywhere
    assert c.get("/v1/workspace", headers=_auth(b)).json()["governed_runs"] == 1


def test_streaming_calls_are_billed_once_the_provider_answers(mods, tmp_path, monkeypatch):
    raw = (
        b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
        b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":9,"completion_tokens":2}}'
        b"\n\ndata: [DONE]\n\n"
    )
    monkeypatch.setattr(
        mods,
        "openai_client_factory",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, content=raw, headers={"content-type": "text/event-stream"}
                )
            )
        ),
    )
    c = _client(mods, tmp_path)
    key = _workspace(c)
    r = _chat(c, key, "s", payload={**CHAT, "stream": True})
    assert r.status_code == 200 and "[DONE]" in r.text
    assert c.get("/v1/workspace", headers=_auth(key)).json()["billed_runs"] == 1


# == x402 credit rail =============================================================================


def _buy(c, key):
    unpaid = c.post("/v1/credits/x402", headers=_auth(key))
    assert unpaid.status_code == 402
    signed = sign_payment(decode_payment_required(unpaid))
    return c.post("/v1/credits/x402", headers={**_auth(key), "PAYMENT-SIGNATURE": signed.header})


def test_x402_purchase_grants_credits_after_settlement(mods, tmp_path):
    fac = FakeFacilitator()
    c = _client(mods, tmp_path, {"BG_X402_PAY_TO": PAY_TO}, facilitator=fac)
    key = _workspace(c)
    r = _buy(c, key)
    assert r.status_code == 200, r.text
    assert [x.method for x in fac.calls] == ["verify", "settle"]
    assert c.get("/v1/workspace", headers=_auth(key)).json()["credits_remaining"] == 1000
    statement = c.get("/v1/workspace/ledger", headers=_auth(key)).json()
    assert statement["grants"][0]["source"] == "x402" and statement["pending_purchases"] == []


def test_x402_settlement_failure_grants_nothing(mods, tmp_path):
    fac = FakeFacilitator(settle_outcomes=[settle_failure()])
    c = _client(mods, tmp_path, {"BG_X402_PAY_TO": PAY_TO}, facilitator=fac)
    key = _workspace(c)
    assert _buy(c, key).status_code == 402
    assert c.get("/v1/workspace", headers=_auth(key)).json()["credits_remaining"] == 0


def test_a_settled_x402_payment_is_not_lost_when_the_grant_crashes(mods, tmp_path, monkeypatch):
    """Settlement succeeded, then the process failed before granting. The pending row survives,
    and reconciliation credits it once the chain shows the authorization was used."""
    app = mods.create_app(
        _settings(mods, tmp_path, {"BG_X402_PAY_TO": PAY_TO}), facilitator=FakeFacilitator()
    )
    c = TestClient(app, raise_server_exceptions=False)
    key = _workspace(c)

    def crash(*_a, **_kw):
        raise RuntimeError("process died after settlement")

    monkeypatch.setattr(app.state.ledger, "settle_pending", crash)
    _buy(c, key)
    assert c.get("/v1/workspace", headers=_auth(key)).json()["credits_remaining"] == 0
    monkeypatch.undo()
    ledger = app.state.ledger
    assert len(ledger.pending()) == 1
    assert ledger.reconcile_pending(lambda _a, _n: True)["settled"] == 1
    assert ledger.reconcile_pending(lambda _a, _n: True)["settled"] == 0
    assert c.get("/v1/workspace", headers=_auth(key)).json()["credits_remaining"] == 1000


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


def test_chain_check_reads_usdc_authorization_state(mods):
    cc = sys.modules["chain_check"]
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": "0x" + "0" * 63 + "1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert (
        cc.authorization_used(
            "http://rpc", "eip155:8453", "0x" + "a" * 40, "0x" + "b" * 64, client=client
        )
        is True
    )
    call = seen["body"]["params"][0]
    assert call["to"] == cc.USDC["eip155:8453"] and call["data"].startswith("0xe94a0102")
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="x")))
    assert (
        cc.authorization_used("http://rpc", "eip155:8453", "0x" + "a" * 40, "0x1", client=down)
        is None
    )


def test_mainnet_credit_sales_need_explicit_approval(mods):
    with pytest.raises(RuntimeError):
        mods.Settings.from_env({"BG_X402_NETWORK": "eip155:8453", "BG_X402_PAY_TO": PAY_TO})
    env = {
        "BG_X402_NETWORK": "eip155:8453",
        "BG_X402_PAY_TO": PAY_TO,
        "BG_X402_MAINNET_APPROVED": "1",
    }
    with pytest.raises(RuntimeError, match="BG_BASE_RPC_URL"):
        mods.Settings.from_env(env)  # no reconciliation source: refuse
    approved = mods.Settings.from_env({**env, "BG_BASE_RPC_URL": "https://rpc.example"})
    assert approved.x402_pay_to == PAY_TO


# == published terms and guards ===================================================================


def test_pricing_and_llms_txt_publish_the_same_terms(mods, tmp_path):
    c = _client(mods, tmp_path, {"BG_X402_PAY_TO": PAY_TO}, facilitator=FakeFacilitator())
    p = c.get("/pricing").json()
    assert p["free_governed_runs_per_month"] == 2 and p["subscription"] is None
    assert p["status"] == "experimental pricing" and "answered" in p["billable_unit"]
    assert p["credits"]["x402"]["price_usd"] == "1.00"
    txt = c.get("/llms.txt").text
    assert "X-Provider-Api-Key" in txt and "/pricing" in txt and "answered" in txt


def test_workspace_creation_is_throttled_per_address(mods, tmp_path):
    c = _client(mods, tmp_path, BG_CREATIONS_PER_IP_PER_HOUR="2")
    assert c.post("/v1/workspaces").status_code == 201
    assert c.post("/v1/workspaces").status_code == 201
    assert c.post("/v1/workspaces").status_code == 429


def test_oversized_bodies_are_refused(mods, tmp_path):
    c = _client(mods, tmp_path, BG_MAX_BODY_BYTES="100")
    key = _workspace(c)
    big = {**CHAT, "messages": [{"role": "user", "content": "x" * 500}]}
    assert _chat(c, key, payload=big).status_code == 413


# == the customer journey =========================================================================


def test_complete_customer_journey(mods, tmp_path, monkeypatch):
    """Discover → workspace → governed work with a budget → enforcement and receipts → allowance
    runs out → buy credits (agent, x402) → continue."""
    calls: list = []
    monkeypatch.setattr(mods, "openai_client_factory", _mock_openai(calls))
    c = _client(mods, tmp_path, {"BG_X402_PAY_TO": PAY_TO}, facilitator=FakeFacilitator())

    terms = c.get("/pricing").json()  # 1. discover and understand the offer
    assert terms["free_governed_runs_per_month"] == 2
    key = c.post("/v1/workspaces").json()["api_key"]  # 2. workspace, no account

    assert _chat(c, key, "job-1", budget="0.50").status_code == 200  # 3. governed work
    blocked = _chat(c, key, "job-2", budget="0.000000001")  # 4. enforcement
    assert blocked.status_code == 402 and blocked.json()["error"]["type"] == "BudgetExceededError"
    receipts = c.get("/v1/work/job-1", headers=_auth(key)).json()
    assert receipts["calls"] == 1 and receipts["receipts"][0]["status"] == "success"

    assert _chat(c, key, "job-3").status_code == 200  # free allowance now used (2 billed)
    out = _chat(c, key, "job-4")  # 5. payment is required, and says how
    assert out.status_code == 402 and out.json()["detail"]["error"] == "allowance_exhausted"
    assert len(calls) == 2  # nothing reached the provider on refusal

    assert _buy(c, key).status_code == 200  # 6. buy credits
    assert _chat(c, key, "job-4").status_code == 200  # 7. continue
    status = c.get("/v1/workspace", headers=_auth(key)).json()
    assert status["billed_runs"] == 3 and status["credits_remaining"] == 999
