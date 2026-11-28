"""Web Read paid from prepaid Hosted credits with an API key (credits_rail + the workspace
ledger's spends). A page costs credits only when it is delivered; a refused page, a missing key,
an unknown key or too few credits costs nothing; a retry with the same request_id is answered
again without a second charge; Hosted's free monthly runs never pay for pages."""

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
ARTICLE = """<html><head><title>Agent payments</title></head><body>
<main><h1>Paying per call</h1><p>Agents pay for results, not subscriptions.</p></main>
</body></html>"""
HTML = {"content-type": "text/html; charset=utf-8"}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


svc = _load("web_read_service", ROOT / "hosted" / "web_read" / "service.py")
fetch = sys.modules["web_read_fetch"]
ws = _load("workspaces", ROOT / "hosted" / "budget_gateway" / "workspaces.py")


def _site() -> httpx.MockTransport:
    routes = {
        "news.test/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
        "news.test/a": httpx.Response(200, text=ARTICLE, headers=HTML),
        "news.test/b": httpx.Response(200, text=ARTICLE.replace("Paying", "Reading"), headers=HTML),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        r = routes.get(f"{request.url.host}{request.url.path}")
        return httpx.Response(404) if r is None else r

    return httpx.MockTransport(handler)


async def _dns(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


class Credits:
    """The cohost adapter's shape (cohost.HostedCredits) over a real WorkspaceLedger."""

    units_per_result = 2
    top_up = {"x402": "POST https://h.test/hosted/v1/credits/x402"}

    def __init__(self, ledger):
        self.ledger = ledger

    def authenticate(self, key):
        return self.ledger.authenticate(key)

    def hold(self, workspace_id, ref, units):
        return self.ledger.hold_spend(workspace_id, ref, units)

    def finish(self, ref, delivered):
        self.ledger.finish_spend(ref, delivered)


@pytest.fixture
def setup(tmp_path):
    ledger = ws.WorkspaceLedger(tmp_path / "w.sqlite3", free_runs_per_month=1000)
    workspace, key = ledger.create_workspace()
    settings = svc.Settings(pay_to=PAY_TO, resource_url="http://testserver/v1/page",
                            data_dir=tmp_path / "read", operator_token="op")  # fmt: skip
    reader = fetch.Fetcher(settings.user_agent, transport=_site(), resolve=_dns)
    app = svc.create_app(settings, fetcher=reader, credits=Credits(ledger))
    return TestClient(app), ledger, workspace, key


def _read(c, key, url="https://news.test/a", request_id=None, header="authorization"):
    headers = {}
    if key and header == "authorization":
        headers = {"authorization": f"Bearer {key}"}
    elif key:
        headers = {"x-api-key": key}
    body = {"url": url, **({"request_id": request_id} if request_id else {})}
    return c.post("/credits/v1/page", json=body, headers=headers)


def test_no_key_or_a_bad_key_is_refused_free_and_says_how_to_start(setup):
    c, ledger, workspace, key = setup
    r = _read(c, None)
    assert r.status_code == 401 and r.json()["error"] == "workspace_key_required"
    assert r.json()["charged"] is False and "x402" in r.json()["get_started"]
    assert _read(c, "irw_not-a-key").status_code == 401


def test_too_few_credits_is_refused_before_reading(setup):
    c, ledger, workspace, key = setup  # 1,000 free Hosted runs, but no prepaid credits
    r = _read(c, key)
    assert r.status_code == 402
    assert r.json() == {"error": "insufficient_credits", "charged": False, "credits_needed": 2,
                        "credits_remaining": 0, "top_up": Credits.top_up}  # fmt: skip


def test_a_delivered_page_costs_its_credits_once_and_a_retry_is_free(setup):
    c, ledger, workspace, key = setup
    ledger.grant("x402", "pay-1", workspace, 10, 1)
    r = _read(c, key, request_id="job-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Agent payments"
    assert body["receipt"]["rail"] == "credits" and body["receipt"]["credits_charged"] == 2
    assert ledger.balance(workspace) == 8
    again = _read(c, key, request_id="job-1", header="x-api-key")
    assert again.status_code == 200 and again.headers["x-inferrail-recovered"] == "1"
    assert again.json()["receipt"]["recovered"] is True
    assert ledger.balance(workspace) == 8  # never charged twice
    assert _read(c, key, url="https://news.test/b", request_id="job-2").status_code == 200
    assert ledger.balance(workspace) == 6


def test_a_page_that_cannot_be_read_costs_nothing(setup):
    c, ledger, workspace, key = setup
    ledger.grant("x402", "pay-1", workspace, 10, 1)
    r = _read(c, key, url="https://news.test/missing")
    assert r.status_code >= 400 and r.json()["charged"] is False
    assert ledger.balance(workspace) == 10


def test_the_x402_route_is_unchanged_next_to_the_credits_route(setup):
    c, ledger, workspace, key = setup
    r = c.post("/v1/page", json={"url": "https://news.test/a"})
    assert r.status_code == 402 and r.headers.get("payment-required")


def test_ledger_spends_hold_bill_release_and_never_use_free_runs(tmp_path):
    ledger = ws.WorkspaceLedger(tmp_path / "w.sqlite3", free_runs_per_month=1000)
    workspace, _ = ledger.create_workspace()
    other, _ = ledger.create_workspace()
    assert ledger.hold_spend(workspace, "r1", 2) == ("insufficient", 0)
    ledger.grant("x402", "p", workspace, 5, 1)
    assert ledger.hold_spend(workspace, "r1", 2) == ("held", 3)
    assert ledger.hold_spend(workspace, "r1", 2)[0] == "busy"
    ledger.finish_spend("r1", delivered=False)
    assert ledger.balance(workspace) == 5  # released
    assert ledger.hold_spend(workspace, "r1", 2) == ("held", 3)
    ledger.finish_spend("r1", delivered=True)
    assert ledger.hold_spend(workspace, "r1", 2) == ("billed", 3)
    with pytest.raises(ValueError):
        ledger.hold_spend(other, "r1", 2)  # a ref never crosses workspaces
    assert ledger.hold_spend(workspace, "r2", 2) == ("held", 1)
    assert ledger.release_orphaned_holds() >= 1  # a crash mid-read returns the hold
    assert ledger.balance(workspace) == 3
    assert ledger.hold_spend(workspace, "r3", 4) == ("insufficient", 3)


def test_cohost_mounts_the_credits_rail_only_when_enabled(tmp_path):
    path = ROOT / "hosted" / "budget_gateway" / "cohost.py"
    cohost = _load("budget_gateway_cohost_credits", path)

    class App:
        class state:  # noqa: N801
            ledger = ws.WorkspaceLedger(tmp_path / "c.sqlite3", free_runs_per_month=1)

    class Hosted:
        public_base_url = "https://h.test/hosted"
        stripe = None

    assert cohost.read_credits_from_env(App, Hosted, env={}) is None
    credits = cohost.read_credits_from_env(App, Hosted, env={"READ_CREDITS": "1"})
    assert credits.units_per_result == 2 and "card" not in credits.top_up
    assert credits.top_up["x402"].startswith("POST https://h.test/hosted/v1/credits/x402")
    with pytest.raises(RuntimeError):
        cohost.read_credits_from_env(App, Hosted, env={"READ_CREDITS": "1",
                                                       "READ_CREDITS_PER_PAGE": "0"})  # fmt: skip


def _origin_app(tmp_path, monkeypatch, credits_on: bool):
    cohost = _load("budget_gateway_cohost_docs", ROOT / "hosted" / "budget_gateway" / "cohost.py")
    monkeypatch.setenv("READ_PAY_TO", PAY_TO)
    monkeypatch.setenv("READ_DATA_DIR", str(tmp_path / "read"))
    if credits_on:
        monkeypatch.setenv("READ_CREDITS", "1")
    else:
        monkeypatch.delenv("READ_CREDITS", raising=False)
    trial_mod, hosted_mod = cohost.load_modules()
    hosted_settings = hosted_mod.Settings.from_env(
        {"BG_DATA_DIR": str(tmp_path / "hosted"), "BG_MOUNT_PREFIX": "/hosted",
         "BG_PUBLIC_BASE_URL": "http://testserver/hosted"}
    )  # fmt: skip
    return cohost.create_combined_app(
        tmp_path / "trial", hosted_settings, modules=(trial_mod, hosted_mod),
        web_search=None, finder=None, packages=None, domains=None, godmode=None,
    )  # fmt: skip


def test_the_api_key_path_is_documented_where_agents_look_when_it_is_on(tmp_path, monkeypatch):
    with TestClient(_origin_app(tmp_path, monkeypatch, credits_on=True)) as c:
        llms = c.get("/llms.txt").text
        assert "/read/credits/v1/page" in llms and "INFERRAIL_API_KEY" in llms
        assert (
            "a person funds it once" in llms
        )  # card funding is a human step; never implied unattended
        op = c.get("/openapi.json").json()["paths"]["/read/credits/v1/page"]["post"]
        assert op["security"] == [{"WorkspaceKey": []}] and "x-payment-info" not in op
        assert "$0.002" in op["description"]
        guide = c.post("/read/v1/page", json={"url": "https://news.test/a"}).json()
        assert "/read/credits/v1/page" in guide["start_paying"]["api_key"]
        r = c.post("/read/credits/v1/page", json={"url": "https://news.test/a"})
        assert r.status_code == 401 and r.json()["error"] == "workspace_key_required"


def test_nothing_advertises_the_api_key_path_when_it_is_off(tmp_path, monkeypatch):
    with TestClient(_origin_app(tmp_path, monkeypatch, credits_on=False)) as c:
        assert "/read/credits/" not in c.get("/llms.txt").text
        assert "/read/credits/v1/page" not in c.get("/openapi.json").json()["paths"]
        guide = c.post("/read/v1/page", json={"url": "https://news.test/a"}).json()
        assert "api_key" not in guide["start_paying"]
