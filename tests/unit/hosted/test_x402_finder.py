"""x402 Finder: paid picks of x402 APIs for a task, ranked by real 30-day unique payers. Proves the
offer, delivery and receipt, ranking and filters, that no match / an unloaded catalog is never
charged, that a retry never pays twice, and that the buyer's `need` is never recorded."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

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
from eth_account import Account  # noqa: E402
from test_web_read import svc  # noqa: E402

finder = svc._sibling("finder")
PAY_TO = "0x" + "8" * 40
SECRET = b"s" * 32
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _item(url, desc, payers, amount="2000", network="eip155:8453", asset=USDC, method="POST"):
    return {"resource": url, "description": desc, "tags": [], "serviceName": "",
            "accepts": [{"scheme": "exact", "network": network, "amount": amount,
                         "asset": asset, "payTo": "0x" + "1" * 40}],
            "extensions": {"bazaar": {"info": {"input": {"method": method, "body": {"url": "x"}}}}},
            "quality": {"l30DaysTotalCalls": payers * 3, "l30DaysUniquePayers": payers,
                        "lastCalledAt": "2026-11-21T00:00:00Z"}}  # fmt: skip


ITEMS = [
    _item("https://a.test/read", "Read a web page as clean markdown", 2),
    _item("https://b.test/md", "Convert any web page to markdown for agents", 400),
    _item("https://c.test/weather", "Current weather for a city", 900),
    _item("https://d.test/pricey", "Web page to markdown, premium", 50, amount="50000"),
    _item(
        "https://e.test/sol", "Read a web page as markdown", 30, network="solana:mainnet", asset="x"
    ),
    {"resource": "http://insecure.test/x", "description": "web page markdown", "accepts": []},
    {"description": "no resource"},
]


def _catalog(items=ITEMS) -> finder.Catalog:
    async def source(offset):
        return items if offset == 0 else []

    cat = finder.Catalog(source)
    asyncio.run(cat.load())
    return cat


def _client(tmp_path: Path, catalog, fac: FakeFacilitator):
    settings = svc.Settings(pay_to=PAY_TO, resource_url="http://testserver/v1/find",
                            data_dir=tmp_path, operator_token="op")  # fmt: skip
    task = finder.finder_task(svc, catalog, SECRET)
    return TestClient(svc.create_app(settings, facilitator=fac, task=task))


def _buy(c: TestClient, body: dict, account=None):
    unpaid = c.post("/v1/find", json=body)
    assert unpaid.status_code == 402, unpaid.text
    signed = sign_payment(decode_payment_required(unpaid), account)
    return c.post("/v1/find", json=body, headers={"PAYMENT-SIGNATURE": signed.header})


def test_unpaid_request_gets_the_offer(tmp_path):
    r = _client(tmp_path, _catalog(), FakeFacilitator()).post("/v1/find", json={"need": "x y"})
    assert r.status_code == 402
    (accept,) = decode_payment_required(r).accepts
    assert accept.amount == "2000" and accept.pay_to == PAY_TO


def test_picks_are_ranked_by_real_payers_and_settle_once(tmp_path):
    fac = FakeFacilitator()
    need = {"need": "read a web page as markdown", "network": "eip155:8453"}
    r = _buy(_client(tmp_path, _catalog(), fac), need)
    assert r.status_code == 200, r.text
    body = r.json()
    urls = [p["url"] for p in body["picks"]]
    # 400 payers outranks closer wording that only 2 wallets paid for
    assert urls[0] == "https://b.test/md"
    assert "https://c.test/weather" not in urls and "http://insecure.test/x" not in urls
    top = body["picks"][0]
    assert (
        top["price_usd"] == "0.002" and top["method"] == "POST" and top["unique_payers_30d"] == 400
    )
    assert body["catalog_size"] == 5 and body["receipt"]["price_usd"] == "0.002"
    assert urls.index("https://b.test/md") < urls.index("https://a.test/read")
    assert len(fac.calls_to("settle")) == 1


def test_network_and_price_filters(tmp_path):
    fac = FakeFacilitator()
    c = _client(tmp_path, _catalog(), fac)
    urls = [
        p["url"]
        for p in _buy(c, {"need": "web page markdown", "max_price_usd": 0.01}).json()["picks"]
    ]
    assert "https://d.test/pricey" not in urls and "https://e.test/sol" not in urls
    sol = _buy(
        c, {"need": "web page markdown", "network": "solana:mainnet", "request_id": "s"}
    ).json()
    assert [p["url"] for p in sol["picks"]] == ["https://e.test/sol"] and sol["picks"][0][
        "price_usd"
    ] is None


def test_no_match_is_never_charged(tmp_path):
    fac = FakeFacilitator()
    r = _buy(_client(tmp_path, _catalog(), fac), {"need": "translate klingon poetry"})
    assert r.status_code == 422 and r.json()["error"] == "no_match" and r.json()["charged"] is False
    assert fac.calls_to("settle") == []


def test_an_unloaded_catalog_is_never_charged(tmp_path):
    async def failing(_offset):
        raise RuntimeError("bazaar down")

    fac = FakeFacilitator()
    r = _buy(_client(tmp_path, finder.Catalog(failing), fac), {"need": "web page markdown"})
    assert r.status_code == 503 and r.json()["error"] == "catalog_loading"
    assert fac.calls_to("settle") == []


def test_a_retry_returns_the_paid_picks_without_a_second_charge(tmp_path):
    fac = FakeFacilitator()
    c = _client(tmp_path, _catalog(), fac)
    body = {"need": "web page markdown", "request_id": "job-1"}
    buyer = Account.create()
    first, again = _buy(c, body, buyer), _buy(c, body, buyer)  # the response was lost; signs again
    assert first.status_code == again.status_code == 200
    assert first.json()["picks_sha256"] == again.json()["picks_sha256"]
    assert len(fac.calls_to("settle")) == 1


def test_the_need_is_never_recorded(tmp_path):
    need = "find the secret merger target filings"
    catalog = _catalog([_item("https://f.test/x", "merger target filings", 3)])
    _buy(_client(tmp_path, catalog, FakeFacilitator()), {"need": need})
    for db in tmp_path.glob("*.sqlite3"):
        dump = "\n".join(sqlite3.connect(db).iterdump())
        assert "merger" not in dump and "secret" not in dump


def test_a_failed_refresh_keeps_the_previous_catalog():
    cat = _catalog()
    before = list(cat.entries)

    async def empty(_offset):
        return []

    cat._source = empty
    asyncio.run(cat.load())
    assert cat.entries == before


def test_a_short_secret_is_refused():
    with pytest.raises(ValueError):
        finder.finder_task(svc, _catalog(), b"short")


# -- mounted next to Web Read and Hosted on the shared origin ------------------------------------
from test_web_search import ROOT, _load  # noqa: E402

cohost = _load("budget_gateway_cohost_finder", ROOT / "hosted" / "budget_gateway" / "cohost.py")
HOSTED_PAY_TO = "0x" + "9" * 40
READ_PAY_TO = "0x" + "7" * 40
ENV = {"FINDER_PAY_TO": PAY_TO, "FINDER_MPP_CHAIN_ID": "42431", "BG_OPERATOR_TOKEN": "op",
       "FINDER_CATALOG_RIGHTS_CONFIRMED": "1"}


def _hosted(tmp_path: Path):
    trial_mod, hosted_mod = cohost.load_modules()
    settings = hosted_mod.Settings.from_env(
        {"BG_DATA_DIR": str(tmp_path / "hosted"), "BG_MOUNT_PREFIX": "/hosted",
         "BG_PUBLIC_BASE_URL": "http://testserver/hosted", "BG_X402_PAY_TO": HOSTED_PAY_TO}
    )  # fmt: skip
    return trial_mod, hosted_mod, settings


def test_not_mounted_without_its_payto(tmp_path):
    _, _, settings = _hosted(tmp_path)
    assert cohost.finder_from_env(settings, {}) is None


@pytest.mark.parametrize(
    "change",
    [
        {"FINDER_CATALOG_RIGHTS_CONFIRMED": None},  # CDP data-use rights not confirmed
        {"FINDER_MPP_CHAIN_ID": None},  # MPP only by default: needs MPP
        {"FINDER_MPP_CHAIN_ID": "4217"},  # Tempo mainnet needs its own approval
        {"FINDER_X402": "1", "FINDER_PAY_TO": HOSTED_PAY_TO},  # x402 needs its own payee
        {"FINDER_X402": "1", "FINDER_PAY_TO": READ_PAY_TO, "READ_PAY_TO": READ_PAY_TO},
        {"BG_OPERATOR_TOKEN": None},  # no stable retry secret
    ],
)
def test_misconfiguration_leaves_it_unmounted(tmp_path, change):
    _, _, settings = _hosted(tmp_path)
    env = {**ENV, "FINDER_DATA_DIR": str(tmp_path / "f")}
    for k, v in change.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    assert cohost.finder_from_env(settings, env) is None


def test_mounted_mpp_only_and_listed_on_the_origin(tmp_path):
    trial_mod, hosted_mod, settings = _hosted(tmp_path)
    finder_app = cohost.finder_from_env(settings, {**ENV, "FINDER_DATA_DIR": str(tmp_path / "f")})
    assert finder_app is not None and finder_app[1].x402 is False
    app = cohost.create_combined_app(
        tmp_path / "trial", settings, facilitator=FakeFacilitator(),
        modules=(trial_mod, hosted_mod), web_read=None, web_search=None, finder=finder_app,
        godmode=None,
    )  # fmt: skip
    c = TestClient(app)
    unpaid = c.post("/find/v1/find", json={"need": "web page markdown"})
    assert unpaid.status_code == 402 and unpaid.headers["www-authenticate"].startswith("Payment ")
    op = c.get("/openapi.json").json()["paths"]["/find/v1/find"]["post"]
    assert op["operationId"] == "findApi" and op["x-payment-info"]["price"]["amount"] == "0.002"
    assert "x402 Finder" in c.get("/llms.txt").text
    info = c.get("/openapi.json").json()["info"]
    assert "/find/v1/find" in info["x-guidance"]
    assert "x402 Finder: which paid x402 API fits a task" in info["description"]


def test_the_approved_production_config_is_mpp_only_at_two_tenths_of_a_cent(tmp_path):
    """Founder-approved: MPP on Tempo mainnet only, $0.002, Web Read's existing recipient."""
    web_read_pay_to = "0xA45140E99E9482a50BC3fA5F6000c02FAAB6F65E"
    _, _, settings = _hosted(tmp_path)
    env = {"FINDER_PAY_TO": web_read_pay_to, "READ_PAY_TO": web_read_pay_to,
           "FINDER_NETWORK": "eip155:8453", "FINDER_MAINNET_APPROVED": "1",
           "FINDER_MPP_CHAIN_ID": "4217", "FINDER_MPP_MAINNET_APPROVED": "1",
           "BG_OPERATOR_TOKEN": "op", "FINDER_DATA_DIR": str(tmp_path / "f"),
           "FINDER_CATALOG_RIGHTS_CONFIRMED": "1"}  # fmt: skip
    mounted = cohost.finder_from_env(settings, env)
    assert mounted is not None
    s = mounted[1]
    assert s.x402 is False and s.price_usd == "0.002" and s.mpp.chain_id == 4217
    assert s.pay_to == web_read_pay_to
    unpaid = TestClient(mounted[0]).post("/v1/find", json={"need": "web page markdown"})
    assert unpaid.status_code == 402
    assert "payment-required" not in unpaid.headers  # no x402 (Base) offer at all
    offers = unpaid.headers.get_list("www-authenticate")
    assert offers and all('method="tempo"' in o for o in offers)
    from mpp import Challenge

    for c in (Challenge.from_www_authenticate(h) for h in offers):
        assert c.request["amount"] == "2000" and c.request["recipient"] == web_read_pay_to


def test_an_unpaid_quote_starts_loading_the_catalog(tmp_path):
    _, _, settings = _hosted(tmp_path)
    mounted = cohost.finder_from_env(settings, {**ENV, "FINDER_DATA_DIR": str(tmp_path / "f")})
    catalog = mounted[0].state.finder_catalog
    catalog._source = _catalog()._source  # no network
    assert catalog._task is None
    assert TestClient(mounted[0]).post("/v1/find", json={"need": "x y"}).status_code == 402
    assert catalog._task is not None  # the first load was started by an unpaid request


def test_the_catalog_status_is_free_and_reports_freshness(tmp_path):
    _, _, settings = _hosted(tmp_path)
    mounted = cohost.finder_from_env(settings, {**ENV, "FINDER_DATA_DIR": str(tmp_path / "f")})
    catalog = mounted[0].state.finder_catalog
    c = TestClient(mounted[0])
    assert c.get("/v1/catalog").json() == {"loaded": False, "entries": 0, "as_of": None}
    catalog._source = _catalog()._source
    asyncio.run(catalog.load())
    status = c.get("/v1/catalog").json()
    assert status["loaded"] is True and status["entries"] == 5 and status["as_of"].endswith("Z")


# -- Base x402 next to Tempo MPP, each paying its own address (founder-approved 11-22) ---------
from _x402_fake_facilitator import authorization_of  # noqa: E402

FINDER_BASE_PAY_TO = "0x54BF9C490112CD6a54Cb8f22D6bf83621ABe77CF"
WEB_READ_PAY_TO = "0xA45140E99E9482a50BC3fA5F6000c02FAAB6F65E"
BOTH = {"FINDER_PAY_TO": WEB_READ_PAY_TO, "READ_PAY_TO": WEB_READ_PAY_TO,
        "FINDER_X402": "1", "FINDER_X402_PAY_TO": FINDER_BASE_PAY_TO,
        "FINDER_NETWORK": "eip155:8453", "FINDER_MAINNET_APPROVED": "1",
        "FINDER_MPP_CHAIN_ID": "4217", "FINDER_MPP_MAINNET_APPROVED": "1",
        "FINDER_CATALOG_RIGHTS_CONFIRMED": "1", "BG_OPERATOR_TOKEN": "op"}  # fmt: skip


def test_the_production_env_splits_recipients_by_rail(tmp_path, monkeypatch):
    monkeypatch.setenv("CDP_API_KEY_ID", "test-key-id")  # Base mainnet settles through CDP
    monkeypatch.setenv("CDP_API_KEY_SECRET", "test-key-secret")
    _, _, settings = _hosted(tmp_path)
    mounted = cohost.finder_from_env(settings, {**BOTH, "FINDER_DATA_DIR": str(tmp_path / "f")})
    assert mounted is not None
    s = mounted[1]
    assert s.x402 is True and s.price_usd == "0.002" and s.mpp.chain_id == 4217
    assert s.accepted() == (("eip155:8453", FINDER_BASE_PAY_TO),)  # Base: the new address only
    assert s.pay_to == WEB_READ_PAY_TO  # MPP (Tempo): unchanged


def test_base_offer_pays_the_new_address_and_tempo_keeps_web_reads(tmp_path):
    rail_mod = sys.modules["web_read_mpp_rail"]
    settings = svc.Settings(
        pay_to=WEB_READ_PAY_TO, x402_pay_to=FINDER_BASE_PAY_TO, network="eip155:8453",
        resource_url="http://testserver/v1/find", data_dir=tmp_path,
        mpp=rail_mod.MppSettings(chain_id=4217, secret_key="k", realm="testserver"),
    )  # fmt: skip
    app = svc.create_app(settings, facilitator=FakeFacilitator(network="eip155:8453"),
                         task=finder.finder_task(svc, _catalog(), SECRET))  # fmt: skip
    unpaid = TestClient(app).post("/v1/find", json={"need": "web page markdown"})
    assert unpaid.status_code == 402
    (accept,) = decode_payment_required(unpaid).accepts
    assert (accept.network, accept.amount, accept.pay_to) == ("eip155:8453", "2000",
                                                             FINDER_BASE_PAY_TO)  # fmt: skip
    from mpp import Challenge

    offers = unpaid.headers.get_list("www-authenticate")
    challenges = [Challenge.from_www_authenticate(h) for h in offers]
    assert challenges
    for c in challenges:
        assert c.request["recipient"] == WEB_READ_PAY_TO and c.request["amount"] == "2000"


def test_a_paid_base_purchase_settles_to_the_new_address_once(tmp_path):
    recipients = []
    fac = FakeFacilitator(on_settle=lambda p: recipients.append(authorization_of(p)["to"]))
    settings = svc.Settings(pay_to=WEB_READ_PAY_TO, x402_pay_to=FINDER_BASE_PAY_TO,
                            resource_url="http://testserver/v1/find", data_dir=tmp_path,
                            operator_token="op")  # fmt: skip
    c = TestClient(svc.create_app(settings, facilitator=fac,
                                  task=finder.finder_task(svc, _catalog(), SECRET)))  # fmt: skip
    buyer = Account.create()
    body = {"need": "web page markdown", "request_id": "job-b"}
    first, again = _buy(c, body, buyer), _buy(c, body, buyer)
    assert first.status_code == again.status_code == 200
    # one settlement, to the new address
    assert [r.lower() for r in recipients] == [FINDER_BASE_PAY_TO.lower()]


def test_a_signature_for_web_reads_address_is_refused(tmp_path):
    fac = FakeFacilitator()
    settings = svc.Settings(
        pay_to=WEB_READ_PAY_TO, x402_pay_to=FINDER_BASE_PAY_TO,
        resource_url="http://testserver/v1/find", data_dir=tmp_path,
    )  # fmt: skip
    task = finder.finder_task(svc, _catalog(), SECRET)
    c = TestClient(svc.create_app(settings, facilitator=fac, task=task))
    unpaid = c.post("/v1/find", json={"need": "web page markdown"})
    offer = decode_payment_required(unpaid)
    offer.accepts[0].pay_to = WEB_READ_PAY_TO  # a buyer signing to the wrong merchant
    signed = sign_payment(offer)
    headers = {"PAYMENT-SIGNATURE": signed.header}
    r = c.post("/v1/find", json={"need": "web page markdown"}, headers=headers)
    assert r.status_code != 200 and fac.calls_to("settle") == []


@pytest.mark.parametrize(
    "change",
    [
        {"FINDER_X402_PAY_TO": WEB_READ_PAY_TO},  # Web Read's address is taken
        {"FINDER_X402_PAY_TO": HOSTED_PAY_TO},  # Hosted's address is taken
        {"FINDER_X402_PAY_TO": None, "FINDER_PAY_TO": WEB_READ_PAY_TO},  # falls back to a taken one
        {"FINDER_X402": "0"},  # an x402 address without x402
        {"FINDER_X402_PAY_TO": "0x1234"},  # not an address
    ],
)
def test_base_misconfiguration_leaves_it_unmounted(tmp_path, change, monkeypatch):
    # with valid CDP keys, so only the change itself can be why it doesn't mount
    monkeypatch.setenv("CDP_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "test-key-secret")
    _, _, settings = _hosted(tmp_path)
    env = {**BOTH, "FINDER_DATA_DIR": str(tmp_path / "f")}
    for k, v in change.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    assert cohost.finder_from_env(settings, env) is None
