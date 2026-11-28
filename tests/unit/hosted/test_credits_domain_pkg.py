"""Domain Check and Package Check paid from the same prepaid workspace credits as Web Read: one API
key, one balance. A delivered lookup costs 2 credits once; a retry with the same request_id is free;
a lookup that can't be served costs nothing; holds are namespaced per product so they never
collide."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("x402")

ROOT = Path(__file__).resolve().parents[3]
PAY_TO = "0x" + "7" * 40


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


svc = sys.modules.get("web_read_service") or _load(
    "web_read_service", ROOT / "hosted" / "web_read" / "service.py"
)
domains = _load("web_read_domains", ROOT / "hosted" / "web_read" / "domains.py")
ws = sys.modules.get("workspaces") or _load(
    "workspaces", ROOT / "hosted" / "budget_gateway" / "workspaces.py"
)


class Credits:
    units_per_result = 2
    top_up = {"card": "POST https://h.test/hosted/v1/credits/checkout"}

    def __init__(self, ledger):
        self.ledger = ledger
        self.refs: list[str] = []

    def authenticate(self, key):
        return self.ledger.authenticate(key)

    def hold(self, workspace_id, ref, units):
        self.refs.append(ref)
        return self.ledger.hold_spend(workspace_id, ref, units)

    def finish(self, ref, delivered):
        self.ledger.finish_spend(ref, delivered)


def doh(fail: bool = False) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(503)
        if "rdap" in str(request.url) or "iana" in str(request.url):
            return httpx.Response(404)
        name, rtype = request.url.params.get("name"), request.url.params.get("type")
        if name.startswith("_dmarc."):
            ans = [{"name": name, "type": 16, "TTL": 300, "data": '"v=DMARC1; p=reject"'}]
        elif rtype == "MX":
            ans = [{"name": name, "type": 15, "TTL": 300, "data": "10 mx.acme.com."}]
        else:
            ans = []
        return httpx.Response(200, json={"Status": 0, "AD": False, "Answer": ans})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def setup(tmp_path):
    ledger = ws.WorkspaceLedger(tmp_path / "w.sqlite3", free_runs_per_month=1000)
    workspace, key = ledger.create_workspace()
    credits = Credits(ledger)

    def app(fail=False):
        settings = svc.Settings(pay_to=PAY_TO, resource_url="http://testserver/v1/lookup",
                                data_dir=tmp_path / ("d-fail" if fail else "d"),
                                operator_token="op")  # fmt: skip
        task = domains.domain_task(svc, b"0" * 32, client=doh(fail))
        return TestClient(svc.create_app(settings, task=task, credits=credits))

    return app, ledger, workspace, key, credits


def _lookup(c, key, request_id="job-1"):
    body = {"domain": "acme.com", "registration": False, "request_id": request_id}
    return c.post("/credits/v1/lookup", json=body,
                  headers={"authorization": f"Bearer {key}"})  # fmt: skip


def test_domain_lookup_paid_from_credits_once_and_retry_free(setup):
    app, ledger, workspace, key, credits = setup
    ledger.grant("x402", "pay-1", workspace, 10, 1)
    c = app()
    r = _lookup(c, key)
    assert r.status_code == 200, r.text
    assert r.json()["email"]["dmarc_policy"] == "reject"
    assert r.json()["receipt"]["credits_charged"] == 2 and r.json()["receipt"]["rail"] == "credits"
    assert r.json()["receipt"]["product"] == "domain lookup"
    again = _lookup(c, key)
    assert again.status_code == 200 and again.headers.get("x-inferrail-recovered") == "1"
    assert ledger.balance(workspace) == 8  # charged once
    assert all(
        ref.startswith("domain-check:") for ref in credits.refs
    )  # own namespace, not web-read:


def test_unservable_lookup_costs_nothing(setup):
    app, ledger, workspace, key, credits = setup
    ledger.grant("x402", "pay-1", workspace, 10, 1)
    r = _lookup(app(fail=True), key, "job-2")
    assert r.status_code != 200 and r.json()["charged"] is False
    assert ledger.balance(workspace) == 10


def test_too_few_credits_and_wording_names_the_product(setup):
    app, ledger, workspace, key, credits = setup
    c = app()
    r = _lookup(c, key, "job-3")
    assert r.status_code == 402 and r.json()["error"] == "insufficient_credits"
    unpaid = c.post("/v1/lookup", json={"domain": "acme.com"})
    guide = unpaid.json().get("start_paying", {}).get("api_key", "")
    assert "per delivered domain lookup" in guide and "/credits/v1/lookup" in guide


def test_tasks_name_their_results():
    from types import SimpleNamespace

    assert domains.domain_task(svc, b"0" * 32).result_noun == "domain lookup"
    packages = _load("web_read_packages", ROOT / "hosted" / "web_read" / "packages.py")
    assert packages.package_task(svc, b"0" * 32).result_noun == "package check"
    assert svc.page_task(SimpleNamespace()).result_noun == "page"
