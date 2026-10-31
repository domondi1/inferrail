"""Inferrail Web Read: a paid per-page reader. These prove what it sells and that a buyer pays
only for a delivered page: the x402 offer, delivery and receipt, no settlement on any refusal,
address safety (SSRF) and robots.txt, and that mounting it can't take Hosted down."""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import httpx
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
from eth_account import Account  # noqa: E402
from x402.http.utils import encode_payment_signature_header  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
PAY_TO = "0x" + "7" * 40
ARTICLE = """<html><head><title>Agent payments</title></head><body>
<nav>Home | Pricing | Login</nav>
<main><h1>Paying per call</h1><p>Agents pay for results, not subscriptions.</p>
<ul><li>Fast</li><li>Verifiable</li></ul></main>
<footer>Copyright</footer><script>track()</script></body></html>"""


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


svc = _load("web_read_service", ROOT / "hosted" / "web_read" / "service.py")
fetch = sys.modules["web_read_fetch"]


def _site(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        r = routes.get(f"{request.url.host}{request.url.path}")
        if r is None:
            return httpx.Response(404)
        return httpx.Response(r.status_code, headers=r.headers, content=r.content)

    return httpx.MockTransport(handler)


async def _public_dns(host: str, port: int) -> list[str]:
    return {"internal.test": ["10.0.0.5"], "localhost": ["127.0.0.1"]}.get(host, ["93.184.216.34"])


def _client(tmp_path: Path, routes: dict[str, httpx.Response], fac: FakeFacilitator):
    settings = svc.Settings(
        pay_to=PAY_TO, resource_url="http://testserver/v1/page", data_dir=tmp_path,
        operator_token="op-token",
    )  # fmt: skip
    reader = fetch.Fetcher(settings.user_agent, transport=_site(routes), resolve=_public_dns)
    return TestClient(svc.create_app(settings, facilitator=fac, fetcher=reader))


def _paid(c: TestClient, url: str):
    unpaid = c.post("/v1/page", json={"url": url})
    assert unpaid.status_code == 402
    signed = sign_payment(decode_payment_required(unpaid))
    return c.post("/v1/page", json={"url": url}, headers={"PAYMENT-SIGNATURE": signed.header})


HTML = {"content-type": "text/html; charset=utf-8"}
ROBOTS_OK = httpx.Response(200, text="User-agent: *\nAllow: /\n")


def test_unpaid_probe_without_a_body_gets_the_exact_offer(tmp_path):
    c = _client(tmp_path, {}, FakeFacilitator())
    r = c.post("/v1/page")  # catalog probes send no body
    assert r.status_code == 402
    req = decode_payment_required(r)
    (accept,) = req.accepts
    assert accept.amount == "2000" and accept.pay_to == PAY_TO  # $0.002 in USDC units
    assert req.resource.url == "http://testserver/v1/page"


def test_a_paid_read_delivers_markdown_with_a_receipt_and_settles_once(tmp_path):
    fac = FakeFacilitator()
    site = {"news.test/robots.txt": ROBOTS_OK,
            "news.test/a": httpx.Response(200, text=ARTICLE, headers=HTML)}  # fmt: skip
    c = _client(tmp_path, site, fac)
    r = _paid(c, "https://news.test/a")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Agent payments"
    assert "# Paying per call" in body["content_markdown"]
    assert "- Fast" in body["content_markdown"]
    assert "Login" not in body["content_markdown"] and "track()" not in body["content_markdown"]
    assert body["receipt"]["payment_ref"] and body["receipt"]["price_usd"] == "0.002"
    assert len(fac.calls_to("settle")) == 1
    stats = c.get("/v1/operator/stats", headers={"Authorization": "Bearer op-token"}).json()
    assert stats["settled_reads"] == 1 and Decimal(stats["revenue_usd"]) == Decimal("0.002")


@pytest.mark.parametrize(
    ("url", "site", "status", "code"),
    [
        ("https://news.test/private", {"news.test/robots.txt": httpx.Response(
            200, text="User-agent: *\nDisallow: /private\n")}, 422, "disallowed_by_robots_txt"),
        ("https://news.test/missing", {"news.test/robots.txt": ROBOTS_OK}, 502, "upstream_status"),
        ("https://news.test/img", {"news.test/robots.txt": ROBOTS_OK, "news.test/img":
            httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})},
         422, "unsupported_content_type"),
        ("https://news.test/big", {"news.test/robots.txt": ROBOTS_OK, "news.test/big":
            httpx.Response(200, content=b"x" * 2_000_001, headers=HTML)}, 422, "too_large"),
        ("http://internal.test/admin", {}, 422, "non_public_address"),
        ("http://localhost/", {}, 422, "non_public_address"),
        ("http://127.0.0.1/", {}, 422, "non_public_address"),
        ("https://news.test:8443/", {}, 422, "unsupported_url"),
        ("file:///etc/passwd", {}, 422, "unsupported_url"),
        ("https://news.test/hop", {"news.test/robots.txt": ROBOTS_OK, "news.test/hop":
            httpx.Response(302, headers={"location": "http://internal.test/meta"})},
         422, "non_public_address"),
    ],
)  # fmt: skip
def test_a_refused_read_is_never_charged(tmp_path, url, site, status, code):
    fac = FakeFacilitator()
    c = _client(tmp_path, site, fac)
    r = _paid(c, url)
    assert r.status_code == status, r.text
    assert r.json()["error"] == code and r.json()["charged"] is False
    assert fac.calls_to("settle") == []


def test_robots_server_error_means_disallowed(tmp_path):
    fac = FakeFacilitator()
    site = {"down.test/robots.txt": httpx.Response(503),
            "down.test/a": httpx.Response(200, text=ARTICLE, headers=HTML)}  # fmt: skip
    r = _paid(_client(tmp_path, site, fac), "https://down.test/a")
    assert r.json()["error"] == "disallowed_by_robots_txt" and fac.calls_to("settle") == []


def test_operator_stats_need_the_token(tmp_path):
    c = _client(tmp_path, {}, FakeFacilitator())
    assert c.get("/v1/operator/stats").status_code == 401


def test_settings_guard_mainnet_and_price():
    base = {"READ_PAY_TO": PAY_TO, "READ_RESOURCE_URL": "https://x/read/v1/page"}
    with pytest.raises(RuntimeError, match="READ_MAINNET_APPROVED"):
        svc.Settings.from_env({**base, "READ_NETWORK": "eip155:8453"})
    with pytest.raises(RuntimeError, match="between"):
        svc.Settings.from_env({**base, "READ_PRICE_USD": "0.0001"})
    assert svc.Settings.from_env(base).price_usd == "0.002"


# -- a retry never pays twice -------------------------------------------------------------------
# Standard x402 clients sign a fresh authorization when they retry after a lost response
# (x402-foundation/x402#3438). The same wallet asking for the same page again inside the recovery
# window must get the paid result back without a second settlement or a second fetch.


def _counting_site(routes: dict[str, httpx.Response]) -> tuple[httpx.MockTransport, list[str]]:
    seen: list[str] = []
    inner = _site(routes)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.host}{request.url.path}")
        return inner.handle_request(request)

    return httpx.MockTransport(handler), seen


def _recovering_client(tmp_path: Path, fac: FakeFacilitator, recovery_s: int = 600):
    transport, seen = _counting_site(
        {"news.test/robots.txt": ROBOTS_OK,
         "news.test/a": httpx.Response(200, text=ARTICLE, headers=HTML)}
    )  # fmt: skip
    settings = svc.Settings(
        pay_to=PAY_TO, resource_url="http://testserver/v1/page", data_dir=tmp_path,
        operator_token="op-token", recovery_s=recovery_s,
    )  # fmt: skip
    reader = fetch.Fetcher(settings.user_agent, transport=transport, resolve=_public_dns)
    return TestClient(svc.create_app(settings, facilitator=fac, fetcher=reader)), seen


def _buy(c: TestClient, body: dict, account=None):
    unpaid = c.post("/v1/page", json=body)
    assert unpaid.status_code == 402
    signed = sign_payment(decode_payment_required(unpaid), account)
    return c.post("/v1/page", json=body, headers={"PAYMENT-SIGNATURE": signed.header}), signed


def test_a_retry_with_a_fresh_signature_gets_the_paid_page_without_paying_again(tmp_path):
    fac = FakeFacilitator()
    c, seen = _recovering_client(tmp_path, fac)
    buyer = Account.create()
    body = {"url": "https://news.test/a", "request_id": "job-7"}
    first, _ = _buy(c, body, buyer)
    assert first.status_code == 200 and len(fac.calls_to("settle")) == 1
    pages_fetched = seen.count("news.test/a")

    retry, signed = _buy(c, body, buyer)  # the response was lost; the client signs again
    assert retry.status_code == 200, retry.text
    assert retry.headers["x-inferrail-recovered"] == "1"
    assert retry.json()["content_sha256"] == first.json()["content_sha256"]
    assert retry.json()["receipt"]["payment_ref"] == first.json()["receipt"]["payment_ref"]
    assert retry.json()["receipt"]["recovered"] is True
    assert len(fac.calls_to("settle")) == 1  # the second signature was never settled
    assert all(call.nonce != signed.nonce for call in fac.calls)  # nor even verified
    assert seen.count("news.test/a") == pages_fetched  # and the page wasn't fetched again
    stats = c.get("/v1/operator/stats", headers={"Authorization": "Bearer op-token"}).json()
    assert stats["settled_reads"] == 1 and stats["funnel"]["recovered"] == 1


def test_resending_the_same_payment_returns_the_page_and_settles_once(tmp_path):
    fac = FakeFacilitator()
    c, _ = _recovering_client(tmp_path, fac)
    body = {"url": "https://news.test/a"}
    first, signed = _buy(c, body)
    again = c.post("/v1/page", json=body, headers={"PAYMENT-SIGNATURE": signed.header})
    assert again.status_code == 200 and again.headers["x-inferrail-recovered"] == "1"
    assert len(fac.calls_to("settle")) == 1


@pytest.mark.parametrize(
    "second",
    [
        {"url": "https://news.test/a", "request_id": "job-8"},  # a new request id is a new read
        {"url": "https://news.test/a?v=2", "request_id": "job-7"},  # so is a different page
    ],
)
def test_a_different_request_is_a_new_paid_read(tmp_path, second):
    fac = FakeFacilitator()
    c, _ = _recovering_client(tmp_path, fac)
    buyer = Account.create()
    _buy(c, {"url": "https://news.test/a", "request_id": "job-7"}, buyer)
    r, _ = _buy(c, second, buyer)
    assert r.status_code == 200 and "x-inferrail-recovered" not in r.headers
    assert len(fac.calls_to("settle")) == 2


def test_another_wallet_cannot_recover_someone_elses_page(tmp_path):
    fac = FakeFacilitator()
    c, _ = _recovering_client(tmp_path, fac)
    body = {"url": "https://news.test/a", "request_id": "job-7"}
    _, original = _buy(c, body)
    r, _ = _buy(c, body)  # same request, different wallet: an ordinary paid read
    assert "x-inferrail-recovered" not in r.headers and len(fac.calls_to("settle")) == 2

    # A header that claims the first payer but is signed by someone else isn't recovered either.
    unpaid = c.post("/v1/page", json=body)
    forged = sign_payment(decode_payment_required(unpaid)).payload.model_copy(deep=True)
    forged.payload["authorization"]["from"] = original.payer
    header = encode_payment_signature_header(forged)
    r = c.post("/v1/page", json=body, headers={"PAYMENT-SIGNATURE": header})
    assert "x-inferrail-recovered" not in r.headers


def test_an_unsettled_read_is_not_recoverable(tmp_path):
    fac = FakeFacilitator(settle_outcomes=[settle_failure()])
    c, _ = _recovering_client(tmp_path, fac)
    buyer = Account.create()
    body = {"url": "https://news.test/a", "request_id": "job-7"}
    first, _ = _buy(c, body, buyer)
    assert first.status_code != 200  # settlement failed, so nothing was sold
    retry, _ = _buy(c, body, buyer)
    assert retry.status_code == 200 and "x-inferrail-recovered" not in retry.headers
    assert len(fac.calls_to("settle")) == 2


def test_after_the_window_the_page_is_deleted_and_a_read_is_paid_again(tmp_path, monkeypatch):
    fac = FakeFacilitator()
    c, _ = _recovering_client(tmp_path, fac, recovery_s=60)
    buyer = Account.create()
    body = {"url": "https://news.test/a", "request_id": "job-7"}
    _buy(c, body, buyer)
    later = svc.time.time() + 61
    monkeypatch.setattr(svc.time, "time", lambda: later)
    r, _ = _buy(c, body, buyer)
    assert "x-inferrail-recovered" not in r.headers and len(fac.calls_to("settle")) == 2
    import sqlite3

    rows = (
        sqlite3.connect(tmp_path / "web_read.sqlite3")
        .execute("SELECT result IS NULL FROM reads ORDER BY created_at")
        .fetchall()
    )
    assert rows[0] == (1,)  # the expired page content is gone from the ledger


def test_an_expired_page_is_deleted_by_any_later_request(tmp_path, monkeypatch):
    c, _ = _recovering_client(tmp_path, FakeFacilitator(), recovery_s=60)
    _buy(c, {"url": "https://news.test/a"})
    later = svc.time.time() + 61
    monkeypatch.setattr(svc.time, "time", lambda: later)
    c.post("/v1/page")  # an unrelated catalog probe
    import sqlite3

    (kept,) = (
        sqlite3.connect(tmp_path / "web_read.sqlite3")
        .execute("SELECT COUNT(*) FROM reads WHERE result IS NOT NULL")
        .fetchone()
    )
    assert kept == 0


def test_a_retry_that_arrives_while_the_first_is_settling_waits_and_is_not_charged(tmp_path):
    import asyncio

    release = asyncio.Event()

    async def slow_settle(_payload):
        await release.wait()

    fac = FakeFacilitator(on_settle=slow_settle)
    c, _ = _recovering_client(tmp_path, fac)
    buyer = Account.create()
    body = {"url": "https://news.test/a", "request_id": "job-7"}
    required = decode_payment_required(c.post("/v1/page", json=body))
    first, second = sign_payment(required, buyer), sign_payment(required, buyer)

    async def run():
        transport = httpx.ASGITransport(app=c.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as a:
            one = asyncio.create_task(
                a.post("/v1/page", json=body, headers={"PAYMENT-SIGNATURE": first.header})
            )
            while not fac.calls_to("settle"):  # the first payment is now settling
                await asyncio.sleep(0.01)
            two = asyncio.create_task(
                a.post("/v1/page", json=body, headers={"PAYMENT-SIGNATURE": second.header})
            )
            await asyncio.sleep(0.05)
            assert not two.done()  # the retry waits for the first instead of paying
            release.set()
            return await one, await two

    r1, r2 = asyncio.run(run())
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.headers["x-inferrail-recovered"] == "1"
    assert len(fac.calls_to("settle")) == 1


def test_the_funnel_separates_price_checks_from_payments_without_recording_payloads(tmp_path):
    fac = FakeFacilitator(verify_invalid_reason=None)
    c, _ = _recovering_client(tmp_path, fac)
    crawler = {"User-Agent": "x402scan-bot/1.0"}
    c.post("/v1/page", headers=crawler)  # a catalog probe: a price check, not a purchase
    c.post("/v1/page", json={"url": "https://news.test/a"}, headers=crawler)
    agent = {"User-Agent": "python-httpx/0.28"}
    unpaid = c.post("/v1/page", json={"url": "https://news.test/a"}, headers=agent)
    signed = sign_payment(decode_payment_required(unpaid))
    paid = c.post("/v1/page", json={"url": "https://news.test/a"},
                  headers={**agent, "PAYMENT-SIGNATURE": signed.header})  # fmt: skip
    assert paid.status_code == 200
    bad = c.post("/v1/page", json={"url": "https://news.test/a"},
                 headers={**agent, "PAYMENT-SIGNATURE": "not-a-payment"})  # fmt: skip
    assert bad.status_code == 402
    c.post("/v1/page", json={"url": "http://localhost/"},
           headers={**agent, "PAYMENT-SIGNATURE": sign_payment(
               decode_payment_required(unpaid)).header})  # fmt: skip

    f = c.get("/v1/operator/stats", headers={"Authorization": "Bearer op-token"}).json()["funnel"]
    assert f["challenge"] == 3 and f["payment_attempt"] == 3 and f["payment_rejected"] == 1
    assert f["settled_reads"] == 1 and f["refused"] == 1
    assert f["by_client"]["x402scan"] == {"challenge": 2}
    assert f["by_client"]["python"]["payment_attempt"] == 3
    import sqlite3

    codes = {row[0] for row in sqlite3.connect(tmp_path / "web_read.sqlite3").execute(
        "SELECT code FROM events")}  # fmt: skip
    assert not any("news.test" in code or "localhost" in code for code in codes)  # no URLs


def test_recovery_can_be_turned_off(tmp_path):
    fac = FakeFacilitator()
    c, _ = _recovering_client(tmp_path, fac, recovery_s=0)
    buyer = Account.create()
    body = {"url": "https://news.test/a", "request_id": "job-7"}
    _buy(c, body, buyer)
    r, _ = _buy(c, body, buyer)
    assert "x-inferrail-recovered" not in r.headers and len(fac.calls_to("settle")) == 2


def test_recovery_window_is_bounded():
    base = {"READ_PAY_TO": PAY_TO, "READ_RESOURCE_URL": "https://x/read/v1/page"}
    assert svc.Settings.from_env(base).recovery_s == 600
    with pytest.raises(RuntimeError, match="READ_RECOVERY_S"):
        svc.Settings.from_env({**base, "READ_RECOVERY_S": "86400"})


# -- mounted next to Hosted on the shared origin ---------------------------------------------------
cohost = _load("budget_gateway_cohost_wr", ROOT / "hosted" / "budget_gateway" / "cohost.py")


def _combined(tmp_path: Path, env: dict[str, str]):
    trial_mod, hosted_mod = cohost.load_modules()
    hosted_settings = hosted_mod.Settings.from_env(
        {"BG_DATA_DIR": str(tmp_path / "hosted"), "BG_MOUNT_PREFIX": "/hosted",
         "BG_PUBLIC_BASE_URL": "http://testserver/hosted", "BG_X402_PAY_TO": "0x" + "9" * 40}
    )  # fmt: skip
    web_read = cohost.web_read_from_env(hosted_settings, env)
    return cohost.create_combined_app(
        tmp_path / "trial", hosted_settings, facilitator=FakeFacilitator(),
        modules=(trial_mod, hosted_mod), web_read=web_read,
    ), web_read  # fmt: skip


def test_mounted_on_the_origin_and_listed_for_catalogs(tmp_path):
    app, web_read = _combined(tmp_path, {"READ_PAY_TO": PAY_TO})
    assert web_read is not None
    with TestClient(app) as c:
        doc = c.get("/openapi.json").json()
        op = doc["paths"]["/read/v1/page"]["post"]
        assert op["security"] == [] and op["x-payment-info"]["price"]["amount"] == "0.002"
        r = c.post("/read/v1/page")
        assert r.status_code == 402
        assert decode_payment_required(r).resource.url == "http://testserver/read/v1/page"
        assert c.get("/hosted/health").status_code == 200
        # The origin now sells two things; its listing says so.
        assert doc["info"]["title"] == "Inferrail"
        assert "Web Read" in doc["info"]["description"]
        assert "/read/v1/page" in doc["info"]["x-guidance"]
        assert "<title>Inferrail</title>" in c.get("/").text


def test_the_listed_request_contract_has_plain_types(tmp_path):
    # Catalogs that route tasks read required fields and types from the schema; an `anyOf`
    # property made one of them record the contract as partial, with no required field.
    app, _ = _combined(tmp_path, {"READ_PAY_TO": PAY_TO})
    with TestClient(app) as c:
        op = c.get("/openapi.json").json()["paths"]["/read/v1/page"]["post"]
        listed = op["requestBody"]["content"]["application/json"]["schema"]
        bazaar = decode_payment_required(c.post("/read/v1/page")).extensions["bazaar"]
        for schema in (listed, bazaar["schema"]["properties"]["input"]["properties"]["body"]):
            assert schema["required"] == ["url"]
            assert {name: prop["type"] for name, prop in schema["properties"].items()} == {
                "url": "string", "request_id": "string",
            }  # fmt: skip
    assert svc.PageRequest.model_validate({"url": "https://a.example", "request_id": None})


@pytest.mark.parametrize(
    "env",
    [
        {"READ_PAY_TO": "0x" + "9" * 40},  # Hosted's own address
        {"READ_PAY_TO": PAY_TO, "SEARCH_PAY_TO": PAY_TO},  # Search's address
        {"READ_PAY_TO": PAY_TO, "READ_NETWORK": "eip155:8453"},  # mainnet without approval
    ],
)
def test_a_bad_web_read_config_leaves_it_unmounted_and_hosted_up(tmp_path, env):
    app, web_read = _combined(tmp_path, env)
    assert web_read is None
    with TestClient(app) as c:
        assert c.post("/read/v1/page").status_code == 404
        assert c.get("/hosted/health").status_code == 200
        assert "/read/v1/page" not in c.get("/openapi.json").json()["paths"]


def test_unset_means_not_mounted(tmp_path):
    app, web_read = _combined(tmp_path, {})
    assert web_read is None


def _get_client(tmp_path: Path, fac: FakeFacilitator, allow_get: bool = True):
    transport, seen = _counting_site(
        {"news.test/robots.txt": ROBOTS_OK,
         "news.test/a": httpx.Response(200, text=ARTICLE, headers=HTML)}
    )  # fmt: skip
    settings = svc.Settings(
        pay_to=PAY_TO, resource_url="http://testserver/v1/page", data_dir=tmp_path,
        operator_token="op-token", allow_get=allow_get,
    )  # fmt: skip
    reader = fetch.Fetcher(settings.user_agent, transport=transport, resolve=_public_dns)
    return TestClient(svc.create_app(settings, facilitator=fac, fetcher=reader)), seen


def _buy_get(c: TestClient, params: dict, account=None):
    unpaid = c.get("/v1/page", params=params)
    assert unpaid.status_code == 402
    signed = sign_payment(decode_payment_required(unpaid), account)
    return c.get("/v1/page", params=params, headers={"PAYMENT-SIGNATURE": signed.header}), signed


def test_get_is_off_unless_the_operator_turns_it_on(tmp_path):
    c, _ = _get_client(tmp_path, FakeFacilitator(), allow_get=False)
    assert c.get("/v1/page", params={"url": "https://news.test/a"}).status_code == 405
    assert "GET " not in c.get("/llms.txt").text
    assert svc.Settings.from_env({"READ_PAY_TO": PAY_TO, "READ_RESOURCE_URL": "http://x/v1/page"}
                                 ).allow_get is False  # fmt: skip


def test_an_unpaid_get_probe_gets_the_same_offer_and_nothing_is_fetched(tmp_path):
    c, seen = _get_client(tmp_path, FakeFacilitator())
    for params in ({}, {"url": "https://news.test/a"}):  # index probes often send no query
        r = c.get("/v1/page", params=params)
        assert r.status_code == 402
        (accept,) = decode_payment_required(r).accepts
        assert accept.amount == "2000" and accept.pay_to == PAY_TO
    assert seen == []  # a crawler or prefetch can't make us read (or charge for) anything
    assert "GET http://testserver/v1/page?url=" in c.get("/llms.txt").text
    assert r.headers["cache-control"] == "no-store"


def test_a_paid_get_delivers_settles_once_and_a_retry_never_pays_twice(tmp_path):
    fac = FakeFacilitator()
    c, seen = _get_client(tmp_path, fac)
    buyer = Account.create()
    params = {"url": "https://news.test/a", "request_id": "job-9"}
    first, _ = _buy_get(c, params, buyer)
    assert first.status_code == 200, first.text
    assert "# Paying per call" in first.json()["content_markdown"]
    assert first.json()["receipt"]["request_id"] == "job-9"
    assert len(fac.calls_to("settle")) == 1

    retry, _ = _buy_get(c, params, buyer)  # lost response; the client signs again
    assert retry.status_code == 200 and retry.headers["x-inferrail-recovered"] == "1"
    assert first.headers["cache-control"] == retry.headers["cache-control"] == "no-store"
    assert len(fac.calls_to("settle")) == 1 and seen.count("news.test/a") == 1


def test_a_refused_or_malformed_get_is_never_charged(tmp_path):
    fac = FakeFacilitator()
    c, _ = _get_client(tmp_path, fac)
    refused, _ = _buy_get(c, {"url": "http://localhost/"})
    assert refused.status_code == 422 and refused.json()["charged"] is False
    unpaid = c.get("/v1/page")
    signed = sign_payment(decode_payment_required(unpaid))
    missing = c.get("/v1/page", headers={"PAYMENT-SIGNATURE": signed.header})
    assert missing.status_code == 422 and missing.json()["charged"] is False
    assert fac.calls_to("settle") == []


@pytest.mark.parametrize("status", [401, 403, 404, 410, 429, 500, 503])
def test_a_target_site_error_is_never_charged_as_the_offer_says(tmp_path, status):
    # The offer (DESCRIPTION, MPP_DESCRIPTION) promises that a blocked, missing or failing page
    # costs nothing; any target status >= 400 must be refused and never settled.
    fac = FakeFacilitator()
    site = {"news.test/robots.txt": ROBOTS_OK, "news.test/x": httpx.Response(status, text="no")}
    r = _paid(_client(tmp_path, site, fac), "https://news.test/x")
    assert r.status_code == 502 and r.json() == {
        "error": "upstream_status", "detail": str(status), "charged": False,
    }  # fmt: skip
    assert fac.calls_to("settle") == []
    assert "403, 404, 5xx" in svc.DESCRIPTION and "403, 404 or 5xx" in svc.MPP_DESCRIPTION


@pytest.mark.parametrize("mpp", [False, True])
def test_an_agent_that_only_knows_the_origin_finds_the_services_from_llms_txt(tmp_path, mpp):
    # Registries and 402s give agents the origin; /llms.txt at its root must lead them to each
    # service's guide and the contract, with the live price and rails.
    pytest.importorskip("mpp.methods.tempo") if mpp else None
    env = {"READ_PAY_TO": PAY_TO, **({"READ_MPP_CHAIN_ID": "42431"} if mpp else {})}
    app, web_read = _combined(tmp_path, env)
    with TestClient(app) as c:
        r = c.get("/llms.txt")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
        text = r.text
        assert "http://testserver/read/llms.txt" in text
        assert "http://testserver/hosted/llms.txt" in text
        assert "http://testserver/openapi.json" in text
        assert f"${web_read[1].price_usd}" in text
        rails = "x402 on Base and MPP on Tempo" if mpp else "x402 on Base"
        assert rails in text and rails in c.get("/openapi.json").json()["info"]["description"]
        assert 'href="/llms.txt"' in c.get("/").text


def test_a_hosted_only_origin_does_not_advertise_web_read(tmp_path):
    app, _ = _combined(tmp_path, {})
    with TestClient(app) as c:
        text = c.get("/llms.txt").text
        assert "Web Read" not in text and "http://testserver/hosted/llms.txt" in text


def test_the_api_catalog_points_catalogs_at_the_openapi_description(tmp_path):
    # RFC 9727 /.well-known/api-catalog, in the exact shape Mercator's catalog reads; the
    # OpenAPI it names must stay reachable with Web Read's x-payment-info.
    app, _ = _combined(tmp_path, {"READ_PAY_TO": PAY_TO})
    with TestClient(app) as c:
        r = c.get("/.well-known/api-catalog")
        assert r.status_code == 200
        assert r.headers["content-type"].split(";")[0] == "application/linkset+json"
        assert r.json() == {
            "linkset": [
                {
                    "anchor": "http://testserver",
                    "service-desc": [
                        {
                            "href": "http://testserver/openapi.json",
                            "type": "application/vnd.oai.openapi+json",
                        }
                    ],
                }
            ]
        }
        doc = c.get("/openapi.json").json()
        payment = doc["paths"]["/read/v1/page"]["post"]["x-payment-info"]
        assert payment["price"] == {"mode": "fixed", "currency": "USD", "amount": "0.002"}
        assert c.post("/read/v1/page").status_code == 402  # the paid route still answers
