"""Inferrail Web Search: a Web Read task sold through the same payment flow.

A fake supplier stands in for Serpex (no network, no credits) and a stub rail for Tempo,
except where a real `mpp` challenge is needed to prove a payment is bound to its route."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("x402")
pytest.importorskip("mpp")

ROOT = Path(__file__).resolve().parents[3]
PAY_TO = "0x" + "7" * 40
PAYER = "0x" + "a" * 40
QUERY = "confidential merger target zqxv industries"
SECRET = b"s" * 32


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


svc = _load("web_read_service", ROOT / "hosted" / "web_read" / "service.py")
search = _load("web_read_search", ROOT / "hosted" / "web_read" / "search.py")
rail_mod = sys.modules["web_read_mpp_rail"]
suppliers, _contract = search.load_suppliers()

HITS = [
    {"title": "Acme", "url": "https://acme.example/", "snippet": "Acme corp"},
    {"title": "News", "url": "https://news.example/a", "snippet": "a story"},
]


class FakeSupplier:
    def __init__(self, fail: bool = False, cogs: int = 800):
        self.fail, self.cogs, self.calls = fail, cogs, []

    async def search(self, request):
        self.calls.append(request)
        if self.fail:
            raise suppliers.SupplierFailure(0)
        return SimpleNamespace(hits=HITS[: request.num_results], cogs=self.cogs)


class StubRail:
    def __init__(self, valid: bool = True, settle_ok: bool = True):
        self.valid, self.settle_ok = valid, settle_ok
        self.validated: list[str | None] = []
        self.settled = 0

    async def challenge_headers(self, url: str, method: str = "POST"):
        return [("WWW-Authenticate", 'Payment id="x", method="tempo", intent="charge"')]

    async def validate(self, authorization: str, url: str | None = None):
        self.validated.append(url)
        return (object(), PAYER, "transaction") if self.valid else "VerificationFailedError"

    async def settle(self, credential):
        if not self.settle_ok:
            raise RuntimeError("broadcast failed")
        self.settled += 1
        return SimpleNamespace(
            reference=f"0x{self.settled:064x}", payer=PAYER, to_payment_receipt=lambda: "r"
        )


def _settings(tmp_path: Path, **kw):
    base = {
        "pay_to": PAY_TO,
        "resource_url": "http://testserver/v1/web",
        "data_dir": tmp_path,
        "operator_token": "op",
        "x402": False,
        "mpp": rail_mod.MppSettings(chain_id=rail_mod.TESTNET, secret_key="k", realm="testserver"),
    }
    return svc.Settings(**{**base, **kw})


def _client(tmp_path: Path, supplier: FakeSupplier, rail: StubRail, **kw):
    task = search.search_task(svc, supplier, SECRET)
    app = svc.create_app(_settings(tmp_path, **kw), tempo_rail=rail, task=task)
    return TestClient(app)


PAY = {"Authorization": "Payment eyJjcmVkZW50aWFsIjoieCJ9"}
BODY = {"query": QUERY, "num_results": 2, "request_id": "r1"}


def _stats(c):
    return c.get("/v1/operator/stats", headers={"Authorization": "Bearer op"}).json()


def test_an_unpaid_search_gets_only_an_mpp_challenge_and_costs_nothing(tmp_path):
    supplier = FakeSupplier()
    r = _client(tmp_path, supplier, StubRail()).post("/v1/web", json=BODY)
    assert r.status_code == 402
    assert r.headers["www-authenticate"].startswith("Payment ")
    assert "payment-required" not in r.headers  # x402 is off for this service
    assert r.headers["cache-control"] == "no-store"
    assert supplier.calls == []


def test_an_x402_payment_is_refused_while_x402_is_off(tmp_path):
    supplier, rail = FakeSupplier(), StubRail()
    c = _client(tmp_path, supplier, rail)
    r = c.post("/v1/web", json=BODY, headers={"PAYMENT-SIGNATURE": "eyJ4IjoxfQ=="})
    assert r.status_code == 402
    assert r.json() == {"error": "x402_not_accepted", "charged": False}
    assert supplier.calls == [] and rail.settled == 0
    assert _stats(c)["funnel"]["attempt_failed"] == {"x402:x402_not_accepted": 1}


def test_a_paid_search_is_delivered_then_broadcast_once(tmp_path):
    supplier, rail = FakeSupplier(), StubRail()
    c = _client(tmp_path, supplier, rail)
    r = c.post("/v1/web", json=BODY, headers=PAY)
    assert r.status_code == 200
    body = r.json()
    assert body["results"] == HITS[:2] and body["result_count"] == 2
    assert body["receipt"]["rail"] == "mpp-tempo" and body["receipt"]["price_usd"] == "0.002"
    assert len(supplier.calls) == 1 and supplier.calls[0].query == QUERY
    assert rail.settled == 1
    assert rail.validated == ["http://testserver/v1/web"]  # the paid route is checked
    s = _stats(c)
    assert s["settled_reads"] == 1 and s["revenue_usd"] == "0.002"


def test_the_query_is_never_recorded(tmp_path):
    c = _client(tmp_path, FakeSupplier(), StubRail())
    r = c.post("/v1/web", json=BODY, headers=PAY)
    assert QUERY not in r.text
    db = sqlite3.connect(tmp_path / "web_read.sqlite3")
    dump = "\n".join(db.iterdump())
    assert QUERY not in dump and "zqxv" not in dump
    assert hashlib.sha256(QUERY.encode()).hexdigest() not in dump  # not even a plain hash
    url, final_url = db.execute("SELECT url, final_url FROM reads").fetchone()
    assert url is None and final_url is None


def test_a_failed_search_is_never_charged(tmp_path):
    supplier, rail = FakeSupplier(fail=True), StubRail()
    c = _client(tmp_path, supplier, rail)
    r = c.post("/v1/web", json=BODY, headers=PAY)
    assert r.status_code == 502
    assert r.json() == {"error": "search_failed", "charged": False}
    assert rail.settled == 0
    assert _stats(c)["settled_reads"] == 0


def test_a_failed_broadcast_returns_no_results(tmp_path):
    c = _client(tmp_path, FakeSupplier(), StubRail(settle_ok=False))
    r = c.post("/v1/web", json=BODY, headers=PAY)
    assert r.status_code == 402 and "results" not in r.json()


def test_a_retry_returns_the_paid_results_without_a_second_search_or_charge(tmp_path):
    supplier, rail = FakeSupplier(), StubRail()
    c = _client(tmp_path, supplier, rail)
    first = c.post("/v1/web", json=BODY, headers=PAY)
    again = c.post("/v1/web", json=BODY, headers=PAY)
    assert again.status_code == 200 and again.json()["receipt"]["recovered"] is True
    assert again.json()["results"] == first.json()["results"]
    assert len(supplier.calls) == 1 and rail.settled == 1


def test_a_different_request_id_is_a_new_purchase(tmp_path):
    supplier, rail = FakeSupplier(), StubRail()
    c = _client(tmp_path, supplier, rail)
    c.post("/v1/web", json=BODY, headers=PAY)
    c.post("/v1/web", json={**BODY, "request_id": "r2"}, headers=PAY)
    assert len(supplier.calls) == 2 and rail.settled == 2


def test_an_invalid_request_is_refused_before_payment(tmp_path):
    supplier, rail = FakeSupplier(), StubRail()
    r = _client(tmp_path, supplier, rail).post("/v1/web", json={"query": " "}, headers=PAY)
    assert r.status_code == 422 and r.json()["charged"] is False
    assert supplier.calls == [] and rail.settled == 0


def test_the_guide_states_the_price_and_rails(tmp_path):
    text = _client(tmp_path, FakeSupplier(), StubRail()).get("/llms.txt").text
    assert "$0.002 per delivered search" in text and "MPP on Tempo" in text
    assert "x402 v2" not in text  # off for this service


def test_a_web_read_credential_cannot_buy_a_search():
    """Real `mpp` challenges: Web Read's and Search's rails share a payee, price and seed."""
    from mpp import Challenge, Credential

    env = {
        "READ_MPP_CHAIN_ID": "42431",
        "WEB_SEARCH_MPP_CHAIN_ID": "42431",
        "BG_OPERATOR_TOKEN": "t",
    }
    read_cfg = rail_mod.MppSettings.from_env(env, realm="h", prefix="READ_")
    search_cfg = rail_mod.MppSettings.from_env(env, realm="h", prefix="WEB_SEARCH_")
    assert read_cfg.secret_key != search_cfg.secret_key
    read = rail_mod.TempoRail(read_cfg, PAY_TO, "0.002")
    web = rail_mod.TempoRail(search_cfg, PAY_TO, "0.002")
    header = asyncio.run(read.challenge_headers("https://h/read/v1/page"))[0][1]
    echo = Challenge.from_www_authenticate(header).to_echo()
    auth = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "c" * 64})
    assert asyncio.run(web.validate(auth.to_authorization(), "https://h/search/v1/web")) == (
        "RouteMismatch"
    )


def test_web_reads_mpp_secret_is_unchanged():
    env = {"BG_OPERATOR_TOKEN": "t"}
    expected = hashlib.sha256(b"inferrail-web-read-mpp|t").hexdigest()
    assert rail_mod._secret(env) == expected == rail_mod._secret(env, "READ_")
    assert rail_mod._secret({**env, "MPP_SECRET_KEY": "m"}, "WEB_SEARCH_") != "m"


def test_search_settings_from_env_guard_mpp_only_and_mainnet(tmp_path):
    env = {
        "WEB_SEARCH_PAY_TO": PAY_TO,
        "WEB_SEARCH_RESOURCE_URL": "https://h/search/v1/web",
        "WEB_SEARCH_X402": "0",
        "WEB_SEARCH_DATA_DIR": str(tmp_path),
    }
    with pytest.raises(RuntimeError, match="WEB_SEARCH_MPP_CHAIN_ID"):
        svc.Settings.from_env(env, prefix="WEB_SEARCH_")
    with pytest.raises(RuntimeError, match="WEB_SEARCH_MPP_MAINNET_APPROVED"):
        svc.Settings.from_env({**env, "WEB_SEARCH_MPP_CHAIN_ID": "4217"}, prefix="WEB_SEARCH_")
    s = svc.Settings.from_env({**env, "WEB_SEARCH_MPP_CHAIN_ID": "42431"}, prefix="WEB_SEARCH_")
    assert s.x402 is False and s.mpp.chain_id == 42431 and s.price_usd == "0.002"


def test_a_short_retry_secret_is_refused():
    with pytest.raises(ValueError, match="secret"):
        search.search_task(svc, FakeSupplier(), b"short")


# -- mounted next to Web Read and Hosted on the shared origin ------------------------------------
from _x402_fake_facilitator import FakeFacilitator  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
cohost = _load("budget_gateway_cohost_ws", ROOT / "hosted" / "budget_gateway" / "cohost.py")
HOSTED_PAY_TO = "0x" + "9" * 40
READ_PAY_TO = "0x" + "8" * 40
ENV = {
    "WEB_SEARCH_PAY_TO": PAY_TO,
    "WEB_SEARCH_SUPPLIER_RIGHTS_CONFIRMED": "1",
    "WEB_SEARCH_SUPPLIER_KEY": "test-key",
    "WEB_SEARCH_SUPPLIER_CREDIT_USD": "0.0008",
    "WEB_SEARCH_MPP_CHAIN_ID": "42431",
    "BG_OPERATOR_TOKEN": "op",
}


def _hosted(tmp_path: Path):
    trial_mod, hosted_mod = cohost.load_modules()
    settings = hosted_mod.Settings.from_env(
        {"BG_DATA_DIR": str(tmp_path / "hosted"), "BG_MOUNT_PREFIX": "/hosted",
         "BG_PUBLIC_BASE_URL": "http://testserver/hosted", "BG_X402_PAY_TO": HOSTED_PAY_TO}
    )  # fmt: skip
    return trial_mod, hosted_mod, settings


def _origin(tmp_path: Path, env: dict[str, str], read_env: dict[str, str] | None = None):
    trial_mod, hosted_mod, settings = _hosted(tmp_path)
    web_read = cohost.web_read_from_env(settings, read_env) if read_env else None
    web_search = cohost.web_search_from_env(
        settings, {**env, "WEB_SEARCH_DATA_DIR": str(tmp_path / "ws")}
    )
    app = cohost.create_combined_app(
        tmp_path / "trial", settings, facilitator=FakeFacilitator(),
        modules=(trial_mod, hosted_mod), web_read=web_read, web_search=web_search,
    )  # fmt: skip
    return app, web_search


def test_not_mounted_without_its_payto():
    assert cohost.web_search_from_env(SimpleNamespace(), {}) is None


@pytest.mark.parametrize(
    "change",
    [
        {"WEB_SEARCH_SUPPLIER_RIGHTS_CONFIRMED": None},  # no written supplier permission
        {"WEB_SEARCH_SUPPLIER_KEY": None},
        {"WEB_SEARCH_SUPPLIER_CREDIT_USD": None},  # cost per search must be known
        {"WEB_SEARCH_PRICE_USD": "0.001", "WEB_SEARCH_SUPPLIER_CREDIT_USD": "0.001"},  # no margin
        {"WEB_SEARCH_SUPPLIER": "exa"},
        {"WEB_SEARCH_MPP_CHAIN_ID": None},  # MPP only by default: needs MPP
        {"WEB_SEARCH_MPP_CHAIN_ID": "4217"},  # Tempo mainnet needs its own approval
        {"WEB_SEARCH_X402": "1", "WEB_SEARCH_PAY_TO": HOSTED_PAY_TO},  # x402 needs its own payee
        {"WEB_SEARCH_X402": "1", "WEB_SEARCH_PAY_TO": READ_PAY_TO, "READ_PAY_TO": READ_PAY_TO},
        {"BG_OPERATOR_TOKEN": None},  # no stable retry secret
    ],
)
def test_misconfiguration_leaves_it_unmounted(tmp_path, change):
    _, _, settings = _hosted(tmp_path)
    env = {**ENV, "WEB_SEARCH_DATA_DIR": str(tmp_path / "ws")}
    for k, v in change.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    assert cohost.web_search_from_env(settings, env) is None


def test_mpp_only_may_share_web_reads_payee_because_payments_are_route_bound(tmp_path):
    _, _, settings = _hosted(tmp_path)
    env = {**ENV, "WEB_SEARCH_PAY_TO": READ_PAY_TO, "READ_PAY_TO": READ_PAY_TO,
           "WEB_SEARCH_DATA_DIR": str(tmp_path / "ws")}  # fmt: skip
    mounted = cohost.web_search_from_env(settings, env)
    assert mounted is not None and mounted[1].x402 is False


def test_mounted_and_listed_with_one_price_everywhere(tmp_path):
    app, web_search = _origin(tmp_path, ENV)
    assert web_search is not None
    settings = web_search[1]
    c = TestClient(app)
    unpaid = c.post("/search/v1/web", json={"query": "q"})
    assert unpaid.status_code == 402 and unpaid.headers["www-authenticate"].startswith("Payment ")
    from mpp import Challenge

    offers = unpaid.headers.get_list("www-authenticate")
    challenges = [Challenge.from_www_authenticate(h) for h in offers]
    assert challenges
    for each in challenges:  # every offer: one price, scoped to this route
        assert each.request["amount"] == "2000"  # $0.002 in 6-decimal units
        assert each.request["_mppx_scope"]["resource"] == "/search/v1/web"
    op = c.get("/openapi.json").json()["paths"]["/search/v1/web"]["post"]
    assert op["x-payment-info"]["price"]["amount"] == settings.price_usd == "0.002"
    assert op["x-payment-info"]["protocols"] == [
        {
            "mpp": {
                "method": "tempo",
                "intent": "charge",
                "currency": op["x-payment-info"]["protocols"][0]["mpp"]["currency"],
            }
        }
    ]  # MPP only: no x402 listed
    assert "$0.002 per delivered search" in op["description"]
    assert "$0.002 per delivered search" in c.get("/search/llms.txt").text
    origin_llms = c.get("/llms.txt").text
    assert "/search/v1/web" in origin_llms and "pay $0.002 over MPP on Tempo" in origin_llms
    assert "Inferrail Web Search" in c.get("/openapi.json").json()["info"]["description"]


def test_web_reads_listing_is_unchanged_when_search_is_mounted(tmp_path):
    read_env = {"READ_PAY_TO": READ_PAY_TO, "READ_DATA_DIR": str(tmp_path / "wr")}
    alone, _ = _origin(tmp_path / "a", {}, read_env)
    both, _ = _origin(tmp_path / "b", ENV, read_env)
    a, b = (
        TestClient(alone).get("/openapi.json").json(),
        TestClient(both).get("/openapi.json").json(),
    )
    assert a["paths"]["/read/v1/page"] == b["paths"]["/read/v1/page"]
    assert set(b["paths"]) - set(a["paths"]) == {"/search/v1/web"}
