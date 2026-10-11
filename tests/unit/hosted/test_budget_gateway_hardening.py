"""Launch hardening for Inferrail Hosted:
- per-workspace rate limit;
- body limit, including chunked uploads;
- the outstanding prepaid-credit cap (separate from the daily cap);
- operator commands (backup, liabilities, refund, reconcile).

No x402 dependency: these run in the main CI job.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from fastapi.testclient import TestClient

HOSTED = Path(__file__).resolve().parents[3] / "hosted" / "budget_gateway"


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


def _client(mods, tmp_path, **env):
    settings = mods.Settings.from_env({"BG_DATA_DIR": str(tmp_path / "data"), **env})
    return TestClient(mods.create_app(settings), raise_server_exceptions=False)


def _key(c):
    return c.post("/v1/workspaces").json()["api_key"]


def test_rate_limit_is_per_workspace(mods, tmp_path):
    c = _client(mods, tmp_path, BG_RATE_LIMIT_PER_MINUTE="3")
    a, b = _key(c), _key(c)
    codes = [
        c.get("/v1/workspace", headers={"Authorization": f"Bearer {a}"}).status_code
        for _ in range(4)
    ]
    assert codes == [200, 200, 200, 429]
    assert c.get("/v1/workspace", headers={"Authorization": f"Bearer {b}"}).status_code == 200


def test_body_limit_also_applies_to_chunked_uploads(mods, tmp_path):
    c = _client(mods, tmp_path, BG_MAX_BODY_BYTES="200")
    key = _key(c)
    headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
    big = json.dumps({"model": "m", "messages": [{"role": "user", "content": "x" * 500}]})

    def chunks(data: str):
        yield data[:100].encode()
        yield data[100:].encode()

    r = c.post("/v1/chat/completions", headers=headers, content=chunks(big))
    assert r.status_code == 413
    small = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    r2 = c.post("/v1/chat/completions", headers=headers, content=chunks(small))
    assert r2.status_code == 400  # parsed and reached the handler: no provider key


STRIPE = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": "whsec_1"}


def test_outstanding_credit_cap_is_separate_from_the_daily_cap(mods, tmp_path, monkeypatch):
    monkeypatch.setattr(
        mods,
        "stripe_client_factory",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"url": "https://s"}))
        ),
    )
    app = mods.create_app(
        mods.Settings.from_env(
            {
                "BG_DATA_DIR": str(tmp_path / "data"),
                **STRIPE,
                "BG_MAX_DAILY_SALES_USD": "1000",
                "BG_MAX_OUTSTANDING_CREDITS_USD": "15",
            }
        )
    )
    c = TestClient(app)
    key = _key(c)
    ws = c.get("/v1/workspace", headers={"Authorization": f"Bearer {key}"}).json()["workspace_id"]
    ledger = app.state.ledger
    ledger.grant("stripe", "old", ws, 10_000, 1000)  # sold 2 days ago, unspent: $10 owed
    ledger._db.execute("UPDATE credit_grants SET granted_at = granted_at - 172800")
    assert ledger.outstanding_liability_cents() == 1000
    h = {"Authorization": f"Bearer {key}"}
    assert c.post("/v1/credits/checkout?packs=1", headers=h).status_code == 503  # 10 + 10 > 15
    ledger.apply_refund("stripe", "old", 1000)  # refunded: nothing owed any more
    assert ledger.outstanding_liability_cents() == 0
    assert c.post("/v1/credits/checkout?packs=1", headers=h).status_code == 200


def test_operator_commands(mods, tmp_path, monkeypatch):
    data = tmp_path / "data"
    app = mods.create_app(mods.Settings.from_env({"BG_DATA_DIR": str(data)}))
    ledger = app.state.ledger
    ws, _ = ledger.create_workspace()
    ledger.grant("x402", "0xp:0xn", ws, 1000, 100)
    ledger.record_pending(
        "x402", "0xp:0xm", ws, 1000, 100, authorizer="0xp", nonce="0xm", valid_before=0
    )
    monkeypatch.setenv("BG_DATA_DIR", str(data))
    ops = _load("budget_gateway_ops", "ops.py")

    copied = ops.backup(str(tmp_path / "backups"))
    restored = sqlite3.connect(copied["workspaces.sqlite3"])
    assert restored.execute("SELECT COUNT(*) FROM credit_grants").fetchone()[0] == 1

    report = ops.liabilities()
    assert report["outstanding_liability_cents"] == 100 + 100  # unspent 1,000 runs + pending
    assert report["net_cash_cents_by_source"] == {"x402": 100}

    # A refund is recorded only with on-chain proof (test_budget_gateway_operator.py covers
    # the proof itself); without the chain settings nothing is recorded.
    monkeypatch.delenv("BG_X402_PAY_TO", raising=False)
    with pytest.raises(SystemExit):
        ops.refund_x402("0xp:0xn", "0x" + "ab" * 32)
    assert ops.liabilities()["net_cash_cents_by_source"] == {"x402": 100}
    monkeypatch.delenv("BG_BASE_RPC_URL", raising=False)
    with pytest.raises(SystemExit):
        ops.reconcile()
