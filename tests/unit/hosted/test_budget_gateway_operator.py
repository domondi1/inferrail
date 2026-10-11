"""Inferrail Hosted operator truth: liabilities, refunds and the commercial report.

- outstanding liability values unspent credits at the price actually paid (never understated);
- an x402 refund is recorded only with an on-chain USDC transfer from the receiving address
  back to the payer; one transaction backs one refund;
- the report separates internal traffic, cash, recognized revenue and deferred revenue, and
  counts refusals and purchase intent without storing request content;
- the operator endpoint is off unless a token is configured.

No x402 dependency except the payment-challenge test.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from fastapi.testclient import TestClient

HOSTED = Path(__file__).resolve().parents[3] / "hosted" / "budget_gateway"
NET = "eip155:8453"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x" + "9" * 40
PAYER = "0x" + "a" * 40
TX = "0x" + "12" * 32
DAY = 86_400
T0 = 1_790_000_000.0


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
    _load("report", "report.py")
    return _load("budget_gateway_service", "service.py")


@pytest.fixture
def ledger(mods, tmp_path):
    return sys.modules["workspaces"].WorkspaceLedger(
        tmp_path / "workspaces.sqlite3", free_runs_per_month=2
    )


def _bill(ledger, ws, work, t):
    a = ledger.hold_run(ws, work, now=t)
    assert a.admitted
    ledger.finish_call(ws, a.month, work, answered=True)


# -- liabilities --------------------------------------------------------------------------------
def test_liability_is_valued_at_the_price_paid_not_a_fixed_default(ledger):
    ws, _ = ledger.create_workspace()
    ledger.grant("x402", "p:1", ws, 500, 100)  # $1 for 500 runs: 0.2 cents per run
    assert ledger.outstanding_liability_cents() == 100  # a fixed 0.1 c/run would say 50
    other, _ = ledger.create_workspace()
    ledger.grant("stripe", "cs_1", other, 3, 1)  # 1 cent for 3 runs rounds UP to a cent
    assert ledger.outstanding_liability_cents() == 101


def test_liability_falls_as_credits_are_consumed_and_never_goes_negative(ledger):
    ws, _ = ledger.create_workspace()
    ledger.grant("x402", "p:1", ws, 10, 10)  # 1 cent per run
    for i in range(2 + 4):  # 2 free runs, then 4 paid
        _bill(ledger, ws, f"r{i}", T0)
    assert ledger.outstanding_liability_cents() == 6
    ledger.apply_refund("x402", "p:1", 10)  # everything refunded: balance negative, owed 0
    assert ledger.outstanding_liability_cents() == 0


# -- refunds need chain proof ---------------------------------------------------------------------
def _topic(addr):
    return "0x" + addr.lower().removeprefix("0x").rjust(64, "0")


def _rpc(receipt, latest=0x100):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        result = receipt if body["method"] == "eth_getTransactionReceipt" else hex(latest)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _receipt(units=1_000_000, sender=PAY_TO, to=PAYER, token=USDC, status="0x1", block=0xF0):
    return {
        "status": status,
        "blockNumber": hex(block),
        "logs": [
            {
                "address": token,
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    _topic(sender),
                    _topic(to),
                ],
                "data": hex(units),
            }
        ],
    }


def test_refund_amount_comes_from_a_confirmed_usdc_transfer_to_the_payer(mods):
    cc = sys.modules["chain_check"]
    assert (
        cc.usdc_refund_cents("https://rpc.example", NET, TX, PAY_TO, PAYER, client=_rpc(_receipt()))
        == 100
    )
    assert (
        cc.usdc_refund_cents(
            "https://rpc.example", NET, TX, PAY_TO, PAYER, client=_rpc(_receipt(5_000))
        )
        == 0
    )
    bad = {
        "not found": _rpc(None),
        "reverted": _rpc(_receipt(status="0x0")),
        "too new": _rpc(_receipt(block=0x100)),
        "wrong sender": _rpc(_receipt(sender="0x" + "1" * 40)),
        "wrong recipient": _rpc(_receipt(to="0x" + "2" * 40)),
        "wrong token": _rpc(_receipt(token="0x" + "3" * 40)),
    }
    for case, client in bad.items():
        with pytest.raises(cc.RefundProofError):
            cc.usdc_refund_cents("https://rpc.example", NET, TX, PAY_TO, PAYER, client=client)
        assert case  # every case refused
    with pytest.raises(cc.RefundProofError):
        cc.usdc_refund_cents(
            "https://rpc.example", NET, "0x1234", PAY_TO, PAYER, client=_rpc(_receipt())
        )


def test_refund_is_recorded_only_with_proof_once_per_transaction(mods, tmp_path, monkeypatch):
    data = tmp_path / "data"
    ledger = sys.modules["workspaces"].WorkspaceLedger(
        data / "workspaces.sqlite3", free_runs_per_month=250
    )
    ws, _ = ledger.create_workspace()
    ref = f"{PAYER}:0xn1"
    ledger.grant("x402", ref, ws, 1000, 100)
    ledger.grant("x402", f"{PAYER}:0xn2", ws, 1000, 100)
    for k, v in {
        "BG_DATA_DIR": str(data),
        "BG_BASE_RPC_URL": "https://rpc.example",
        "BG_X402_NETWORK": NET,
        "BG_X402_PAY_TO": PAY_TO,
    }.items():
        monkeypatch.setenv(k, v)
    ops = _load("budget_gateway_ops", "ops.py")
    proven = {TX: 40, "0x" + "34" * 32: 60}
    checked: list = []

    def fake_chain(rpc, network, tx, sender, recipient, **_):
        checked.append((network, sender, recipient))
        if tx not in proven:
            raise ops.chain_check.RefundProofError("no USDC transfer")
        return proven[tx]

    monkeypatch.setattr(ops.chain_check, "usdc_refund_cents", fake_chain)

    with pytest.raises(SystemExit, match="no USDC transfer"):
        ops.refund_x402(ref, "0x" + "ff" * 32)
    assert ops.liabilities()["net_cash_cents_by_source"] == {"x402": 200}

    partial = ops.refund_x402(ref, TX)
    assert partial["verified_cents"] == 40 and partial["runs_reversed_total"] == 400
    assert ops.refund_x402(ref, TX)["runs_reversed_total"] == 400  # re-run: no double count
    with pytest.raises(SystemExit, match="already backs"):
        ops.refund_x402(f"{PAYER}:0xn2", TX)  # one tx can't back two refunds
    rest = ops.refund_x402(ref, "0x" + "34" * 32)
    assert rest["runs_reversed_total"] == 1000  # 40 + 60 cents: fully refunded
    assert ops.liabilities()["net_cash_cents_by_source"] == {"x402": 100}
    assert set(checked) == {(NET, PAY_TO, PAYER)}  # from our address, to the original payer
    with pytest.raises(SystemExit, match="ref must be"):
        ops.refund_x402("not-a-ref", TX)


# -- the report -----------------------------------------------------------------------------------
def test_report_separates_internal_traffic_cash_and_revenue(mods, ledger):
    report = sys.modules["report"]
    internal, _ = ledger.create_workspace()
    a, _ = ledger.create_workspace()  # external, activated, buys, returns
    b, _ = ledger.create_workspace()  # external, never runs anything
    ledger._db.execute("UPDATE workspaces SET created_at=?", (int(T0),))
    _bill(ledger, internal, "x", T0 + 60)
    _bill(ledger, a, "r1", T0 + 3600)  # first answered run one hour after creation
    _bill(ledger, a, "r2", T0 + 3600)
    ledger.grant("x402", f"{PAYER}:0x1", a, 1000, 100)  # $1, 0.1 c/run
    ledger.grant("x402", f"{PAYER}:0x2", a, 1000, 100)  # repeat purchase, same wallet
    ledger.grant("x402", "0x" + "b" * 40 + ":0x3", a, 1000, 100)  # a test wallet
    for i in range(10):  # 10 paid runs, 8 days later
        _bill(ledger, a, f"p{i}", T0 + 8 * DAY)
    ledger.count_event(a, "budget_refused", now=T0)
    ledger.count_event(a, "budget_refused", now=T0)
    ledger.count_event(a, "allowance_exhausted", now=T0)
    ledger.count_event(internal, "budget_refused", now=T0)
    ledger.count_event("", "x402_challenge", now=T0)
    ledger.count_event(a, "x402_challenge", now=T0)
    ledger.record_pending(
        "x402", f"{PAYER}:0x9", a, 1000, 100, authorizer=PAYER, nonce="0x9", valid_before=0
    )

    r = report.build(
        ledger,
        exclude_workspaces=report.parse_labels(f"{internal}:founder"),
        exclude_wallets=report.parse_labels("0x" + "B" * 40 + ":test"),
        now=T0 + 31 * DAY,
    )
    assert r["excluded"] == {"workspaces_by_label": {"founder": 1}, "wallets": 1, "grants": 1}
    assert r["workspaces"]["external"] == 2 and r["workspaces"]["all_including_excluded"] == 3
    assert r["activation"]["workspaces_with_an_answered_run"] == 1
    assert r["activation"]["time_to_first_answered_run_hours"]["median"] == 1.0
    u = r["usage"]
    assert u["answered_runs"] == 12 and u["paid_answered_runs"] == 10
    assert u["budget_refused_calls"] == 2 and u["allowance_exhausted_refusals"] == 1
    assert u["workspaces_that_exhausted_the_free_allowance"] == 1
    assert u["workspaces_used_on_2_plus_days"] == 1
    assert r["retention"]["day_7"] == {
        "eligible_activated_workspaces": 1,
        "retained": 1,
        "status": "measured",
    }
    assert r["retention"]["day_30"]["retained"] == 0
    p = r["purchases"]
    assert p["payment_challenges_served"] == 1
    assert p["payment_challenges_without_a_workspace_key"] == 1
    assert p["settled_purchases"] == 2 and p["distinct_paying_wallets"] == 1
    assert p["repeat_paying_wallets"] == 1 and p["signed_x402_attempts"]["pending"] == 1
    m = r["money_cents"]
    assert m["gross_sales"] == 200 and m["net_cash_received"] == 200
    # External credits: 2,000 runs for 200 cents. 10 consumed by billed runs = 1 cent
    # recognized; the 1,990 unspent are deferred (a liability), not revenue.
    assert m["recognized_revenue"] == 1
    assert m["deferred_revenue_unspent_credits"] == 199
    assert m["pending_purchases"] == 100
    assert r["costs_cents"] == "unknown: none supplied"
    assert r["contribution_margin_cents"] is None
    assert "revenue" in r["accounting_rule"] and r["limitations"]


def test_report_reads_no_request_content(mods, ledger):
    """The report has ids, counts, amounts and times, and nothing else to read."""
    tables = {
        row[0] for row in ledger._db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert tables == {
        "workspaces",
        "governed_runs",
        "credit_grants",
        "credit_reversals",
        "pending_purchases",
        "refund_proofs",
        "usage_events",
    }


# -- the service counts and serves it -------------------------------------------------------------
def _client(mods, tmp_path, facilitator=None, **env):
    settings = mods.Settings.from_env({"BG_DATA_DIR": str(tmp_path / "data"), **env})
    return TestClient(
        mods.create_app(settings, facilitator=facilitator), raise_server_exceptions=False
    )


def _chat(c, key, work, budget=None):
    h = {
        "Authorization": f"Bearer {key}",
        "X-Provider-Api-Key": "sk-test-0123456789",
        "X-Inferrail-Attribute-Work-Id": work,
    }
    if budget:
        h["X-Inferrail-Budget-Usd"] = budget
    return c.post(
        "/v1/chat/completions",
        headers=h,
        json={
            "model": "gpt-4o-mini",
            "max_tokens": 20,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )


def test_service_counts_refusals_and_serves_the_report_only_to_the_operator(
    mods, tmp_path, monkeypatch
):
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": "gpt-4o-mini",
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    monkeypatch.setattr(
        mods,
        "openai_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(answer)),
    )
    assert _client(mods, tmp_path / "off").get("/v1/operator/report").status_code == 404
    c = _client(mods, tmp_path, BG_FREE_RUNS_PER_MONTH="1", BG_OPERATOR_TOKEN="op-secret")
    key = c.post("/v1/workspaces").json()["api_key"]
    internal = c.post("/v1/workspaces").json()["workspace_id"]
    assert _chat(c, key, "tiny", budget="0.0000001").status_code == 402  # budget refusal
    assert _chat(c, key, "a").status_code == 200
    assert _chat(c, key, "b").status_code == 402  # allowance exhausted
    assert c.get("/v1/operator/report").status_code == 401
    bad = c.get("/v1/operator/report", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    r = c.get(
        f"/v1/operator/report?exclude={internal}:test",
        headers={"Authorization": "Bearer op-secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["workspaces"]["external"] == 1
    assert body["excluded"]["workspaces_by_label"] == {"test": 1}
    assert body["usage"]["budget_refused_calls"] == 1
    assert body["usage"]["allowance_exhausted_refusals"] == 1
    assert body["activation"]["workspaces_with_an_answered_run"] == 1
    assert "/v1/operator/report" not in c.get("/openapi.json").text


def test_unpaid_payment_challenges_are_counted_as_purchase_intent(mods, tmp_path):
    pytest.importorskip("x402")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _x402_fake_facilitator import FakeFacilitator

    c = _client(
        mods,
        tmp_path,
        facilitator=FakeFacilitator(),
        BG_X402_PAY_TO=PAY_TO,
        BG_OPERATOR_TOKEN="op",
    )
    key = c.post("/v1/workspaces").json()["api_key"]
    assert c.post("/v1/credits/x402", headers={"Authorization": f"Bearer {key}"}).status_code == 402
    assert c.post("/v1/credits/x402").status_code == 402
    p = c.get("/v1/operator/report", headers={"Authorization": "Bearer op"}).json()["purchases"]
    assert p["payment_challenges_served"] == 1
    assert p["payment_challenges_without_a_workspace_key"] == 1
