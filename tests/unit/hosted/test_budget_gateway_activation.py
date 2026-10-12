"""Inferrail Hosted activation: a new customer or agent can get from zero to a governed run, and
recover from ordinary errors, using only what the service itself publishes.

- the Anthropic SDK's default credential header (`x-api-key`) works as the workspace key, and the
  provider still receives only the provider key;
- 429 carries Retry-After;
- allowance_exhausted says what to do when no purchase rail is open, and when the allowance
  resets;
- error remediation speaks to Hosted customers, not self-hosted config;
- /pricing and /llms.txt publish absolute URLs and the complete calling contract.

No x402 dependency except the one test that publishes an x402 rail.
"""

from __future__ import annotations

import calendar
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from fastapi.testclient import TestClient

HOSTED = Path(__file__).resolve().parents[3] / "hosted" / "budget_gateway"
PROVIDER_KEY = "sk-ant-test-provider-key-0123456789"
BASE = "https://hosted.example/hosted"


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


def _client(mods, tmp_path, facilitator=None, **env):
    settings = mods.Settings.from_env(
        {"BG_DATA_DIR": str(tmp_path / "data"), "BG_PUBLIC_BASE_URL": BASE, **env}
    )
    return TestClient(
        mods.create_app(settings, facilitator=facilitator), raise_server_exceptions=False
    )


def _key(c):
    return c.post("/v1/workspaces").json()["api_key"]


def _mock_anthropic(calls: list):
    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
        )

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


MESSAGE = {
    "model": "claude-haiku-4-5",
    "max_tokens": 20,
    "messages": [{"role": "user", "content": "hi"}],
}


def test_anthropic_sdk_default_header_is_the_workspace_key(mods, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(mods, "anthropic_client_factory", _mock_anthropic(calls))
    c = _client(mods, tmp_path)
    key = _key(c)
    # What `anthropic.Anthropic(base_url=..., api_key=<workspace key>)` sends.
    r = c.post(
        "/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "X-Provider-Api-Key": PROVIDER_KEY,
            "X-Inferrail-Attribute-Work-Id": "sdk-run",
            "X-Inferrail-Budget-Usd": "0.50",
        },
        json=MESSAGE,
    )
    assert r.status_code == 200, r.text
    assert len(calls) == 1
    upstream = calls[0].headers
    assert upstream["x-api-key"] == PROVIDER_KEY  # the provider never sees the workspace key
    assert key not in json.dumps(dict(upstream))
    usage = c.get("/v1/workspace", headers={"x-api-key": key}).json()
    assert usage["governed_runs"] == 1


def test_a_wrong_key_in_either_header_is_rejected_with_guidance(mods, tmp_path):
    c = _client(mods, tmp_path)
    for headers in ({}, {"x-api-key": "irw_nope"}, {"Authorization": "Bearer irw_nope"}):
        r = c.get("/v1/workspace", headers=headers)
        assert r.status_code == 401
        assert r.json()["detail"].endswith("POST https://hosted.example/hosted/v1/workspaces")


def test_rate_limited_responses_say_when_to_retry(mods, tmp_path):
    c = _client(mods, tmp_path, BG_RATE_LIMIT_PER_MINUTE="2")
    key = _key(c)
    h = {"Authorization": f"Bearer {key}"}
    assert [c.get("/v1/workspace", headers=h).status_code for _ in range(2)] == [200, 200]
    r = c.get("/v1/workspace", headers=h)
    assert r.status_code == 429
    assert 1 <= int(r.headers["retry-after"]) <= 61


def test_exhausted_allowance_without_a_purchase_rail_says_so(mods, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(mods, "anthropic_client_factory", _mock_anthropic(calls))
    c = _client(mods, tmp_path, BG_FREE_RUNS_PER_MONTH="1")
    key = _key(c)

    def run(work):
        return c.post(
            "/v1/messages",
            headers={
                "x-api-key": key,
                "X-Provider-Api-Key": PROVIDER_KEY,
                "X-Inferrail-Attribute-Work-Id": work,
            },
            json=MESSAGE,
        )

    assert run("a").status_code == 200
    r = run("b")
    assert r.status_code == 402
    d = r.json()["detail"]
    assert d["error"] == "allowance_exhausted" and d["purchase"] == {}
    assert "not open" in d["message"] and "Buy credits" not in d["message"]
    assert d["free_allowance_resets_at"].endswith("-01T00:00:00Z")
    assert d["pricing_url"] == BASE + "/pricing"
    assert len(calls) == 1


def test_allowance_reset_rolls_over_the_year(mods):
    dec = calendar.timegm((2026, 12, 15, 12, 0, 0))
    assert mods._next_month_utc(dec) == "2027-01-01T00:00:00Z"
    assert mods._next_month_utc(1_791_000_000) == "2026-11-01T00:00:00Z"


def test_budget_refusal_remediation_is_for_hosted_customers(mods, tmp_path):
    c = _client(mods, tmp_path)
    key = _key(c)
    r = c.post(
        "/v1/messages",
        headers={
            "x-api-key": key,
            "X-Provider-Api-Key": PROVIDER_KEY,
            "X-Inferrail-Attribute-Work-Id": "tiny",
            "X-Inferrail-Budget-Usd": "0.0000001",
        },
        json=MESSAGE,
    )
    assert r.status_code == 402
    err = r.json()["error"]
    assert err["code"] == "INFERRAIL_E010"
    assert "inferrail.yaml" not in err["remediation"]
    assert "inferrail budget set" not in err["remediation"]
    assert "X-Inferrail-Budget-Usd" in err["remediation"]


def test_every_hosted_remediation_avoids_self_hosted_instructions(mods):
    for code, text in mods._HOSTED_REMEDIATION.items():
        assert code.startswith("INFERRAIL_E")
        for self_hosted in ("inferrail.yaml", "api_key_env", "inferrail config", "routes:"):
            assert self_hosted not in text, (code, self_hosted)


def test_published_contract_is_complete_and_absolute(mods, tmp_path):
    c = _client(mods, tmp_path)
    p = c.get("/pricing").json()
    assert p["api_base_url"] == BASE + "/v1"
    assert p["credit_purchases_open"] is False and p["credits"] == {}
    assert p["docs"] == {"llms_txt": BASE + "/llms.txt", "openapi": BASE + "/openapi.json"}
    assert p["per_run_budget_max_usd"] == "100"
    guide = c.get("/llms.txt").text
    for needle in (
        f"POST {BASE}/v1/workspaces",
        "Authorization: Bearer <api_key>",
        "x-api-key: <api_key>",
        "X-Provider-Api-Key",
        "X-Inferrail-Attribute-Work-Id",
        "X-Inferrail-Budget-Usd",
        f"OpenAI SDK base_url = {BASE}/v1",
        f"Anthropic SDK base_url = {BASE},",
        f"GET {BASE}/v1/work/<work id>",
        "Credit purchases are not open on this deployment yet.",
        "Retry-After",
        "pip install inferrail",
    ):
        assert needle in guide, needle
    assert "PAYMENT-SIGNATURE" not in guide  # no payment instructions while no rail is open


def test_published_contract_describes_the_x402_rail_when_it_is_open(mods, tmp_path):
    pytest.importorskip("x402")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _x402_fake_facilitator import FakeFacilitator

    pay_to = "0x" + "9" * 40
    c = _client(mods, tmp_path, facilitator=FakeFacilitator(), BG_X402_PAY_TO=pay_to)
    p = c.get("/pricing").json()
    rail = p["credits"]["x402"]
    assert p["credit_purchases_open"] is True
    assert rail["url"] == BASE + "/v1/credits/x402" and rail["pay_to"] == pay_to
    guide = c.get("/llms.txt").text
    assert f"POST {BASE}/v1/credits/x402" in guide and pay_to in guide
    assert "PAYMENT-REQUIRED" in guide and "PAYMENT-SIGNATURE" in guide
    assert "do NOT sign a new payment" in guide
    assert "not open" not in guide


def test_x402_challenge_carries_valid_discovery_metadata(mods, tmp_path):
    pytest.importorskip("x402")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _x402_fake_facilitator import FakeFacilitator, decode_payment_required
    from x402.extensions.bazaar import validate_and_extract

    c = _client(mods, tmp_path, facilitator=FakeFacilitator(), BG_X402_PAY_TO="0x" + "9" * 40)
    key = _key(c)
    r = c.post("/v1/credits/x402", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 402
    bazaar = (decode_payment_required(r).extensions or {})["bazaar"]
    assert bazaar["info"]["input"]["method"] == "POST"
    assert bazaar["info"]["output"]["example"]["governed_runs"] == 1000
    assert validate_and_extract(bazaar).valid
