"""Cohosting tests: Inferrail Hosted mounted at /hosted inside the trial gateway's process.

These prove:
- the trial (/try/ API) still works through the combined app;
- the mounted x402 credit route is still payment-protected;
- credentials don't cross between the two apps;
- both lifespans run;
- money state stays in Hosted's own data directory;
- the daily sales cap and the trusted client-IP header work.
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

pytest.importorskip("x402")
pytest.importorskip("eth_account")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _x402_fake_facilitator import (  # noqa: E402
    FakeFacilitator,
    decode_payment_required,
    sign_payment,
)

COHOST = Path(__file__).resolve().parents[3] / "hosted" / "budget_gateway" / "cohost.py"
PAY_TO = "0x" + "9" * 40
PROVIDER_KEY = "sk-test-provider-key-0123456789abcdef"


def _load_cohost() -> ModuleType:
    spec = importlib.util.spec_from_file_location("budget_gateway_cohost", COHOST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["budget_gateway_cohost"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def parts(monkeypatch, tmp_path):
    monkeypatch.setenv("COST_GATEWAY_ISSUE_MAX_PER_IP", "1000")
    cohost = _load_cohost()
    trial_mod, hosted_mod = cohost.load_modules()

    def build(**env):
        settings = hosted_mod.Settings.from_env(
            {
                "BG_DATA_DIR": str(tmp_path / "hosted"),
                "BG_MOUNT_PREFIX": "/hosted",
                "BG_PUBLIC_BASE_URL": "http://testserver/hosted",
                "BG_FREE_RUNS_PER_MONTH": "1",
                "BG_X402_PAY_TO": PAY_TO,
                **env,
            }
        )
        app = cohost.create_combined_app(
            tmp_path / "trial",
            settings,
            facilitator=FakeFacilitator(),
            modules=(trial_mod, hosted_mod),
        )
        return app

    return build, trial_mod, hosted_mod, tmp_path


def _mock_openai():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": body["model"],
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _hosted_key(c):
    r = c.post("/hosted/v1/workspaces")
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


def test_trial_api_still_works_through_the_combined_app(parts):
    build, *_ = parts
    with TestClient(build()) as c:
        assert c.get("/health").json() == {"status": "ok"}
        trial = c.post("/v1/trial")
        assert trial.status_code == 200
        token = trial.json()["api_key"]
        demo = c.post(
            "/v1/demo/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": "demo", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert demo.status_code == 200
        assert c.get("/hosted/health").json() == {"status": "ok"}


def test_mounted_credit_route_is_still_payment_protected(parts):
    build, *_ = parts
    with TestClient(build()) as c:
        key = _hosted_key(c)
        unpaid = c.post("/hosted/v1/credits/x402", headers={"Authorization": f"Bearer {key}"})
        assert unpaid.status_code == 402  # not a handler 400: the x402 gate matched the route
        req = decode_payment_required(unpaid)
        assert req.resource.url == "http://testserver/hosted/v1/credits/x402"
        signed = sign_payment(req)
        paid = c.post(
            "/hosted/v1/credits/x402",
            headers={"Authorization": f"Bearer {key}", "PAYMENT-SIGNATURE": signed.header},
        )
        assert paid.status_code == 200, paid.text
        status = c.get("/hosted/v1/workspace", headers={"Authorization": f"Bearer {key}"}).json()
        assert status["credits_remaining"] == 1000
        assert c.get("/hosted/pricing").json()["credits"]["x402"]["endpoint"] == (
            "POST /hosted/v1/credits/x402"
        )


def test_hosted_governed_call_works_through_the_mount(parts, monkeypatch):
    build, _, hosted_mod, _ = parts
    monkeypatch.setattr(hosted_mod, "openai_client_factory", _mock_openai())
    with TestClient(build()) as c:
        key = _hosted_key(c)
        r = c.post(
            "/hosted/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "X-Provider-Api-Key": PROVIDER_KEY,
                "X-Inferrail-Attribute-Work-Id": "job",
                "X-Inferrail-Budget-Usd": "0.50",
            },
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert r.status_code == 200, r.text
        assert (
            c.get("/hosted/v1/work/job", headers={"Authorization": f"Bearer {key}"}).json()["calls"]
            == 1
        )


def test_credentials_do_not_cross_between_the_apps(parts):
    build, *_ = parts
    with TestClient(build()) as c:
        trial_token = c.post("/v1/trial").json()["api_key"]
        hosted_key = _hosted_key(c)
        assert (
            c.get(
                "/hosted/v1/workspace", headers={"Authorization": f"Bearer {trial_token}"}
            ).status_code
            == 401
        )
        assert (
            c.get("/v1/receipts", headers={"Authorization": f"Bearer {hosted_key}"}).status_code
            == 401
        )


def test_money_state_lives_only_in_the_hosted_data_directory(parts):
    build, _, _, tmp_path = parts
    with TestClient(build()) as c:
        _hosted_key(c)
        c.post("/v1/trial")
    trial_files = {p.name for p in (tmp_path / "trial").iterdir()}
    hosted_files = {p.name for p in (tmp_path / "hosted").iterdir()}
    assert "workspaces.sqlite3" in hosted_files
    assert not any("workspaces" in f or f == "instance.lock" for f in trial_files)


def test_both_lifespans_run_including_reconciliation(parts, monkeypatch):
    build, _, hosted_mod, _ = parts
    seen = []
    monkeypatch.setattr(
        hosted_mod,
        "authorization_checker",
        lambda settings: lambda a, n: seen.append((a, n)) or None,
    )
    app = build()
    ledger = app.state.hosted_app.state.ledger
    ws, _ = ledger.create_workspace()
    ledger.record_pending(
        "x402", "p:1", ws, 1000, 100, authorizer="0xa", nonce="0x1", valid_before=2_000_000_000
    )
    with TestClient(app):
        pass  # startup ran: the trial purge loop started, Hosted reconciled once
    assert seen == [("0xa", "0x1")]


def test_daily_sales_cap_bounds_exposure(parts):
    build, *_ = parts
    with TestClient(build(BG_MAX_DAILY_SALES_USD="1")) as c:
        key = _hosted_key(c)

        def buy():
            unpaid = c.post("/hosted/v1/credits/x402", headers={"Authorization": f"Bearer {key}"})
            signed = sign_payment(decode_payment_required(unpaid))
            return c.post(
                "/hosted/v1/credits/x402",
                headers={"Authorization": f"Bearer {key}", "PAYMENT-SIGNATURE": signed.header},
            )

        assert buy().status_code == 200
        assert buy().status_code == 503  # second $1 sale would exceed the $1/day cap
        status = c.get("/hosted/v1/workspace", headers={"Authorization": f"Bearer {key}"}).json()
        assert status["credits_remaining"] == 1000


def test_creation_throttle_uses_the_trusted_client_ip_header(parts):
    build, *_ = parts
    with TestClient(build(BG_CREATIONS_PER_IP_PER_HOUR="1")) as c:
        assert (
            c.post("/hosted/v1/workspaces", headers={"True-Client-IP": "198.51.100.1"}).status_code
            == 201
        )
        assert (
            c.post("/hosted/v1/workspaces", headers={"True-Client-IP": "198.51.100.1"}).status_code
            == 429
        )
        assert (
            c.post("/hosted/v1/workspaces", headers={"True-Client-IP": "198.51.100.2"}).status_code
            == 201
        )


def test_cohost_refuses_a_wrong_mount_prefix(parts, monkeypatch):
    _, _, hosted_mod, _ = parts
    cohost = sys.modules["budget_gateway_cohost"]
    monkeypatch.setenv("BG_MOUNT_PREFIX", "/other")
    with pytest.raises(RuntimeError):
        cohost.settings_from_env(hosted_mod)


def test_shutdown_releases_hosted_writer_lock_for_a_replacement_app(parts):
    build, _, hosted_mod, tmp_path = parts
    with TestClient(build()):
        with pytest.raises(hosted_mod.InstanceLockError):
            build()
    with TestClient(build()) as client:
        assert client.get("/hosted/health").status_code == 200
    assert (tmp_path / "hosted" / "workspaces.sqlite3").is_file()


def test_failed_startup_releases_hosted_writer_lock(parts, monkeypatch):
    from contextlib import asynccontextmanager

    build, *_ = parts
    app = build()

    @asynccontextmanager
    async def failing_startup(_):
        raise RuntimeError("trial startup failed")
        yield

    monkeypatch.setattr(app.state.trial_app.router, "lifespan_context", failing_startup)
    with pytest.raises(RuntimeError, match="trial startup failed"):
        with TestClient(app):
            pass
    with TestClient(build()) as client:
        assert client.get("/hosted/health").status_code == 200


def test_origin_openapi_lists_hosted_operations_for_x402_catalogs(parts):
    # x402scan registers an endpoint only if the origin's /openapi.json lists it, and reads
    # the price from x-payment-info. An agent must be able to go from the listing to a 402.
    build, *_ = parts
    with TestClient(build()) as c:
        doc = c.get("/openapi.json").json()
        assert doc["openapi"].startswith("3.")
        assert doc["info"]["title"] and doc["info"]["version"]
        assert "/v1/trial" in doc["paths"]  # the trial's schema is still served

        signup = doc["paths"]["/hosted/v1/workspaces"]["post"]
        assert signup["security"] == []
        assert "x-payment-info" not in signup
        assert c.post("/hosted/v1/workspaces").status_code == 201

        buy = doc["paths"]["/hosted/v1/credits/x402"]["post"]
        assert buy["x-payment-info"] == {
            "price": {"mode": "fixed", "currency": "USD", "amount": "1.00"},
            "protocols": [{"x402": {}}],
        }
        (requirement,) = buy["security"]
        (scheme,) = requirement
        assert doc["components"]["securitySchemes"][scheme]["type"] == "apiKey"
        for op in (signup, buy):
            assert op["summary"] and op["responses"]
        # The catalog probe sends no key and no body; it must still reach the challenge.
        assert c.post("/hosted/v1/credits/x402").status_code == 402
        assert doc["x-agentcash-guidance"]["llmsTxtUrl"] == "http://testserver/hosted/llms.txt"


def test_origin_openapi_omits_the_purchase_when_x402_is_off(parts):
    build, *_ = parts
    with TestClient(build(BG_X402_PAY_TO="")) as c:
        paths = c.get("/openapi.json").json()["paths"]
        assert "/hosted/v1/workspaces" in paths
        assert "/hosted/v1/credits/x402" not in paths


def test_origin_openapi_includes_operations_added_by_a_cohosted_app(parts):
    build, *_ = parts
    app = build()
    app.state.discovery_paths.append(lambda: {"/other/search": {"post": {"summary": "x"}}})
    with TestClient(app) as c:
        paths = c.get("/openapi.json").json()["paths"]
        assert paths["/other/search"] == {"post": {"summary": "x"}}
        assert "/hosted/v1/credits/x402" in paths
