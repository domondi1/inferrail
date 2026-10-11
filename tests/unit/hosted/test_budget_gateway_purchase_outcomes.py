"""What a paying agent sees after it sends PAYMENT-SIGNATURE, and what it can look up.

Every outcome must tell the agent, programmatically, whether it was charged and whether paying
again is safe:
- 200 means settled and credited, with the payment_ref;
- a settlement failure (402) leaves a `pending` purchase the agent can find by payment_ref,
  which becomes `failed` (not charged; pay again) or `settled` (credited once) from chain truth;
- a replayed payment is never credited twice;
- a payment rejected before settlement is not listed (not charged).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("x402")
pytest.importorskip("eth_account")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _x402_fake_facilitator import (  # noqa: E402
    FakeFacilitator,
    decode_payment_required,
    settle_failure,
    sign_payment,
)

HOSTED = Path(__file__).resolve().parents[3] / "hosted" / "budget_gateway"
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
    _load("chain_check", "chain_check.py")
    _load("report", "report.py")
    return _load("budget_gateway_service", "service.py")


def _setup(mods, tmp_path, fac):
    settings = mods.Settings.from_env(
        {"BG_DATA_DIR": str(tmp_path / "data"), "BG_X402_PAY_TO": PAY_TO}
    )
    app = mods.create_app(settings, facilitator=fac)
    c = TestClient(app, raise_server_exceptions=False)
    key = c.post("/v1/workspaces").json()["api_key"]
    return app, c, {"Authorization": f"Bearer {key}"}


def _sign(c, auth):
    unpaid = c.post("/v1/credits/x402", headers=auth)
    assert unpaid.status_code == 402
    return sign_payment(decode_payment_required(unpaid))


def _pay(c, auth, signed):
    return c.post("/v1/credits/x402", headers={**auth, "PAYMENT-SIGNATURE": signed.header})


def _ref(signed):
    return f"{signed.payer.lower()}:{signed.nonce.lower()}"


def _purchases(c, auth):
    return {
        p["payment_ref"]: p for p in c.get("/v1/workspace/ledger", headers=auth).json()["purchases"]
    }


def test_a_200_means_settled_and_credited_with_the_payment_ref(mods, tmp_path):
    _, c, auth = _setup(mods, tmp_path, FakeFacilitator())
    signed = _sign(c, auth)
    r = _pay(c, auth, signed)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "settled" and body["payment_ref"] == _ref(signed)
    assert r.headers.get("payment-response")  # the x402 settlement receipt header
    assert c.get("/v1/workspace", headers=auth).json()["credits_remaining"] == 1000
    assert _purchases(c, auth)[_ref(signed)]["state"] == "settled"


def test_a_replayed_payment_is_never_credited_twice(mods, tmp_path):
    _, c, auth = _setup(mods, tmp_path, FakeFacilitator())
    signed = _sign(c, auth)
    assert _pay(c, auth, signed).status_code == 200
    again = _pay(c, auth, signed)
    assert again.status_code == 402  # the chain refuses a used authorization
    assert again.json().get("status") != "settled"
    assert c.get("/v1/workspace", headers=auth).json()["credits_remaining"] == 1000
    assert len(_purchases(c, auth)) == 1


def test_a_failed_settlement_is_findable_and_resolves_from_chain_truth(mods, tmp_path):
    app, c, auth = _setup(mods, tmp_path, FakeFacilitator(settle_outcomes=[settle_failure()]))
    signed = _sign(c, auth)
    r = _pay(c, auth, signed)
    assert r.status_code == 402 and r.json().get("status") != "settled"
    entry = _purchases(c, auth)[_ref(signed)]
    assert entry["state"] == "pending"  # the agent waits; it must not pay again yet
    assert c.get("/v1/workspace", headers=auth).json()["credits_remaining"] == 0

    # The chain says the authorization was never used, and it has expired: not charged.
    app.state.ledger.reconcile_pending(lambda a, n: False, now=entry["valid_before"] + 3600)
    assert _purchases(c, auth)[_ref(signed)]["state"] == "failed"

    # Paying again is safe and credits exactly once.
    retry = _sign(c, auth)
    assert _pay(c, auth, retry).status_code == 200
    assert c.get("/v1/workspace", headers=auth).json()["credits_remaining"] == 1000
    states = {ref: p["state"] for ref, p in _purchases(c, auth).items()}
    assert states == {_ref(signed): "failed", _ref(retry): "settled"}


def test_a_failed_settlement_that_actually_landed_is_credited_once(mods, tmp_path):
    """The settle call failed (e.g. timeout), but the transfer happened on-chain."""
    app, c, auth = _setup(mods, tmp_path, FakeFacilitator(settle_outcomes=[settle_failure()]))
    signed = _sign(c, auth)
    assert _pay(c, auth, signed).status_code == 402
    app.state.ledger.reconcile_pending(lambda a, n: True)
    app.state.ledger.reconcile_pending(lambda a, n: True)  # repeated passes are harmless
    assert _purchases(c, auth)[_ref(signed)]["state"] == "settled"
    assert c.get("/v1/workspace", headers=auth).json()["credits_remaining"] == 1000


def test_a_payment_rejected_before_settlement_is_not_listed(mods, tmp_path):
    fac = FakeFacilitator(verify_invalid_reason="invalid_exact_evm_insufficient_balance")
    _, c, auth = _setup(mods, tmp_path, fac)
    signed = _sign(c, auth)
    assert _pay(c, auth, signed).status_code == 402
    assert [x.method for x in fac.calls] == ["verify"]  # never settled
    assert _purchases(c, auth) == {}
    assert c.get("/v1/workspace", headers=auth).json()["credits_remaining"] == 0


def test_the_agent_guide_spells_out_every_outcome(mods, tmp_path):
    _, c, _auth = _setup(mods, tmp_path, FakeFacilitator())
    guide = c.get("/llms.txt").text
    for needle in (
        "payment_ref = '<payer>:<nonce>'",
        "HTTP 200 (status settled)",
        "do NOT sign a new payment yet",
        "pending: wait",
        "failed, or not listed: you were not charged",
        "HTTP 503",
    ):
        assert needle in guide, needle


def test_production_sales_caps_refuse_before_any_money_moves(mods, tmp_path):
    """The launch values: $1 packs, BG_MAX_DAILY_SALES_USD=25, BG_MAX_OUTSTANDING_CREDITS_USD=50.
    A refused purchase gets 503 and is never settled, so the buyer is not charged."""
    fac = FakeFacilitator()
    settings = mods.Settings.from_env(
        {
            "BG_DATA_DIR": str(tmp_path / "data"),
            "BG_X402_PAY_TO": PAY_TO,
            "BG_MAX_DAILY_SALES_USD": "25",
            "BG_MAX_OUTSTANDING_CREDITS_USD": "50",
        }
    )
    app = mods.create_app(settings, facilitator=fac)
    c = TestClient(app, raise_server_exceptions=False)
    auth = {"Authorization": f"Bearer {c.post('/v1/workspaces').json()['api_key']}"}
    codes = [_pay(c, auth, _sign(c, auth)).status_code for _ in range(26)]
    assert codes == [200] * 25 + [503]
    assert len(fac.calls_to("settle")) == 25  # the 26th was never settled
    assert c.get("/v1/workspace", headers=auth).json()["credits_remaining"] == 25_000

    # A day later the daily cap has room again, but $25 of unspent credit is still owed:
    # 25 more sales are allowed up to $50 outstanding, then refused.
    ledger = app.state.ledger
    ledger._db.execute("UPDATE credit_grants SET granted_at = granted_at - 90000")
    ledger._db.execute("UPDATE pending_purchases SET created_at = created_at - 90000")
    codes = [_pay(c, auth, _sign(c, auth)).status_code for _ in range(26)]
    assert codes == [200] * 25 + [503]
    assert ledger.outstanding_liability_cents() == 5000
    assert len(fac.calls_to("settle")) == 50
