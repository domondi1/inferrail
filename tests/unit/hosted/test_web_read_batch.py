"""Web Read batch (`POST /read/v1/pages`): up to 10 pages in one paid request, $0.002 per page,
all or nothing, sharing Web Read's receiving address.

Proves: the offer is N x the page price on x402 and MPP; validation happens before any offer;
every page delivered -> one settlement of exactly N x price; any page failing -> nothing settled
and no page content returned; a retry never pays twice; and the shared address is safe:
a single-page payment can't buy a batch, one batch's payment can't buy another, a payment for N
pages can't buy N+1, another wallet can't recover someone's batch, MPP challenges are bound to
the route and the amount, and Web Read's single-page sale is unchanged next to it."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount, Route

pytest.importorskip("x402")
pytest.importorskip("eth_account")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _x402_fake_facilitator import (  # noqa: E402
    FakeFacilitator,
    authorization_of,
    decode_payment_required,
    sign_payment,
)
from eth_account import Account  # noqa: E402
from test_web_read import ARTICLE, HTML, ROBOTS_OK, _public_dns, fetch, svc  # noqa: E402

batch = svc._sibling("batch")
rail_mod = sys.modules["web_read_mpp_rail"]
PAY_TO = "0x" + "7" * 40  # one Web Read address for both endpoints
PAYER = "0x" + "a" * 40


JS_ONLY = "<html><body><div id=root></div><script>x()</script></body></html>"


def _page(title: str) -> httpx.Response:
    return httpx.Response(200, headers=HTML, text=ARTICLE.replace("Agent payments", title))


def _site(seen: list[str]) -> httpx.MockTransport:
    routes = {"news.test/robots.txt": ROBOTS_OK, "news.test/a": _page("A"),
              "news.test/b": _page("B"), "news.test/c": _page("C"),
              "news.test/js": httpx.Response(200, headers=HTML, text=JS_ONLY)}  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"
        seen.append(key)
        r = routes.get(key)
        return httpx.Response(404) if r is None else httpx.Response(
            r.status_code, headers=r.headers, content=r.content)  # fmt: skip

    return httpx.MockTransport(handler)


def _origin(tmp_path: Path, fac: FakeFacilitator, rail: object | None = None):
    """Web Read and its batch on one origin, exactly as the shared host mounts them."""
    seen: list[str] = []
    reader = fetch.Fetcher("test", transport=_site(seen), resolve=_public_dns)
    page_settings = svc.Settings(pay_to=PAY_TO, resource_url="http://testserver/read/v1/page",
                                 data_dir=tmp_path / "page", mount_prefix="/read",
                                 operator_token="op")  # fmt: skip
    batch_settings = svc.Settings(pay_to=PAY_TO, resource_url="http://testserver/read/v1/pages",
                                  data_dir=tmp_path / "batch", mount_prefix="",
                                  operator_token="op")  # fmt: skip
    page_app = svc.create_app(page_settings, facilitator=fac, fetcher=reader,
                              **({"tempo_rail": rail} if rail else {}))  # fmt: skip
    batch_app = svc.create_app(batch_settings, facilitator=fac, tempo_rail=rail,
                               task=batch.batch_task(svc, reader))  # fmt: skip
    app = Starlette(routes=[Route("/read/v1/pages", endpoint=batch_app, methods=["POST"]),
                            Mount("/read", app=page_app)])  # fmt: skip
    return TestClient(app), seen


A, B, C = "https://news.test/a", "https://news.test/b", "https://news.test/c"


def _quote(c: TestClient, body: dict):
    r = c.post("/read/v1/pages", json=body)
    assert r.status_code == 402, r.text
    return decode_payment_required(r)


def _buy(c: TestClient, body: dict, account=None, path="/read/v1/pages"):
    unpaid = c.post(path, json=body)
    assert unpaid.status_code == 402, unpaid.text
    signed = sign_payment(decode_payment_required(unpaid), account)
    return c.post(path, json=body, headers={"PAYMENT-SIGNATURE": signed.header}), signed


@pytest.mark.parametrize(("urls", "amount"), [([A], "2000"), ([A, B, C], "6000"),
                                              ([f"https://news.test/{i}" for i in range(10)],
                                               "20000")])  # fmt: skip
def test_the_offer_is_n_times_the_page_price(tmp_path, urls, amount):
    c, seen = _origin(tmp_path, FakeFacilitator())
    (accept,) = _quote(c, {"urls": urls}).accepts
    assert accept.amount == amount and accept.pay_to == PAY_TO
    assert seen == []  # nothing is fetched before payment


@pytest.mark.parametrize(
    "body",
    [{"urls": []}, {"urls": [A] * 2}, {"urls": [f"https://news.test/{i}" for i in range(11)]},
     {"urls": "https://news.test/a"}, {"urls": [""]}, {"url": A}, {}],
)  # fmt: skip
def test_an_invalid_request_gets_no_offer_and_is_never_charged(tmp_path, body):
    fac = FakeFacilitator()
    c, seen = _origin(tmp_path, fac)
    r = c.post("/read/v1/pages", json=body)
    assert r.status_code == 422 and r.json() == {"error": "invalid_request", "charged": False}
    assert seen == [] and fac.calls == []


def test_a_bare_price_check_with_no_body_gets_the_one_page_offer(tmp_path):
    # CDP Bazaar's validator and health probe POST with no body; they must see an offer.
    fac = FakeFacilitator()
    c, seen = _origin(tmp_path, fac)
    r = c.post("/read/v1/pages")
    assert r.status_code == 402, r.text
    (accept,) = decode_payment_required(r).accepts
    assert accept.amount == "2000" and accept.pay_to == PAY_TO
    assert seen == [] and fac.calls_to("settle") == []


def test_a_paid_request_with_no_body_is_still_refused_and_never_settled(tmp_path):
    fac = FakeFacilitator()
    c, seen = _origin(tmp_path, fac)
    signed = sign_payment(_quote(c, {"urls": [A]}))
    r = c.post("/read/v1/pages", headers={"PAYMENT-SIGNATURE": signed.header})
    assert r.status_code == 422 and r.json()["charged"] is False
    assert seen == [] and fac.calls_to("settle") == []


def test_a_delivered_batch_settles_once_for_exactly_n_pages(tmp_path):
    amounts = []
    fac = FakeFacilitator(on_settle=lambda p: amounts.append(authorization_of(p)["value"]))
    c, _ = _origin(tmp_path, fac)
    r, _ = _buy(c, {"urls": [B, A, C], "request_id": "job-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [p["title"] for p in body["pages"]] == ["B", "A", "C"]  # the order given
    assert body["count"] == 3 and all(p["metadata"] is not None for p in body["pages"])
    assert body["receipt"]["price_usd"] == "0.006" and body["receipt"]["request_id"] == "job-1"
    assert [str(a) for a in amounts] == ["6000"] and len(fac.calls_to("settle")) == 1


def test_one_failing_page_means_nothing_is_settled_and_no_content_is_returned(tmp_path):
    fac = FakeFacilitator()
    c, _ = _origin(tmp_path, fac)
    r, _ = _buy(c, {"urls": [A, "https://news.test/missing", "https://news.test/js"]})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"] == "pages_failed" and body["charged"] is False
    assert fac.calls_to("settle") == []
    assert [(x["url"], x["delivered"], x.get("error")) for x in body["results"]] == [
        (A, True, None), ("https://news.test/missing", False, "upstream_status"),
        ("https://news.test/js", False, "no_readable_content")]  # fmt: skip
    assert "content_markdown" not in r.text and "Paying per call" not in r.text


def test_the_buyer_can_resend_without_the_failed_urls(tmp_path):
    fac = FakeFacilitator()
    c, _ = _origin(tmp_path, fac)
    _buy(c, {"urls": [A, "https://news.test/missing"]})
    r, _ = _buy(c, {"urls": [A]})
    assert r.status_code == 200 and r.json()["count"] == 1
    assert len(fac.calls_to("settle")) == 1


def test_a_lost_response_is_recovered_without_a_second_charge(tmp_path):
    fac = FakeFacilitator()
    c, seen = _origin(tmp_path, fac)
    buyer = Account.create()
    body = {"urls": [A, B], "request_id": "job-7"}
    first, _ = _buy(c, body, buyer)
    again, _ = _buy(c, body, buyer)  # a fresh signature for the same batch
    assert first.status_code == again.status_code == 200
    assert again.json()["receipt"].get("recovered") is True
    assert first.json()["content_sha256"] == again.json()["content_sha256"]
    assert len(fac.calls_to("settle")) == 1 and seen.count("news.test/a") == 1


def test_another_wallet_cannot_recover_someone_elses_batch(tmp_path):
    fac = FakeFacilitator()
    c, _ = _origin(tmp_path, fac)
    body = {"urls": [A, B], "request_id": "same"}
    _buy(c, body, Account.create())
    r, _ = _buy(c, body, Account.create())
    assert r.status_code == 200 and "recovered" not in r.json()["receipt"]
    assert len(fac.calls_to("settle")) == 2  # a second buyer pays for its own batch


def test_a_different_batch_is_a_new_purchase(tmp_path):
    fac = FakeFacilitator()
    c, _ = _origin(tmp_path, fac)
    buyer = Account.create()
    _buy(c, {"urls": [A, B], "request_id": "x"}, buyer)
    r, _ = _buy(c, {"urls": [B, A], "request_id": "x"}, buyer)  # same pages, another order
    assert r.status_code == 200 and "recovered" not in r.json()["receipt"]
    assert len(fac.calls_to("settle")) == 2


# -- the shared address: one payment buys exactly one request --------------------------------
def test_a_settled_batch_payment_cannot_buy_another_batch(tmp_path):
    fac = FakeFacilitator()
    c, seen = _origin(tmp_path, fac)
    first, signed = _buy(c, {"urls": [A, B]})
    assert first.status_code == 200
    fetched = len(seen)
    replay = c.post("/read/v1/pages", json={"urls": [B, C]},
                    headers={"PAYMENT-SIGNATURE": signed.header})  # fmt: skip
    assert replay.status_code == 402 and "Paying per call" not in replay.text
    # Refused at verification (the nonce is spent): nothing fetched, nothing settled again.
    assert len(seen) == fetched and len(fac.calls_to("settle")) == 1
    assert len(fac.settled_nonces) == 1


def test_a_single_page_payment_cannot_buy_a_batch(tmp_path):
    fac = FakeFacilitator()
    c, _ = _origin(tmp_path, fac)
    page, signed = _buy(c, {"url": A}, path="/read/v1/page")
    assert page.status_code == 200
    for urls in ([A], [A, B]):  # same amount (settled nonce) and a larger amount
        r = c.post("/read/v1/pages", json={"urls": urls},
                   headers={"PAYMENT-SIGNATURE": signed.header})  # fmt: skip
        assert r.status_code == 402 and "Paying per call" not in r.text
    assert len(fac.settled_nonces) == 1


def test_an_unspent_single_page_payment_does_not_cover_a_larger_batch(tmp_path):
    fac = FakeFacilitator()
    c, seen = _origin(tmp_path, fac)
    signed = sign_payment(decode_payment_required(c.post("/read/v1/page", json={"url": A})))
    r = c.post("/read/v1/pages", json={"urls": [A, B]},
               headers={"PAYMENT-SIGNATURE": signed.header})  # fmt: skip
    assert r.status_code == 402 and seen == [] and fac.calls_to("settle") == []


def test_a_payment_for_n_pages_cannot_buy_n_plus_one(tmp_path):
    fac = FakeFacilitator()
    c, seen = _origin(tmp_path, fac)
    signed = sign_payment(_quote(c, {"urls": [A, B]}))
    r = c.post("/read/v1/pages", json={"urls": [A, B, C]},
               headers={"PAYMENT-SIGNATURE": signed.header})  # fmt: skip
    assert r.status_code == 402 and seen == [] and fac.calls_to("settle") == []


def test_a_batch_payment_cannot_buy_a_single_page_after_it_settled(tmp_path):
    fac = FakeFacilitator()
    c, _ = _origin(tmp_path, fac)
    first, signed = _buy(c, {"urls": [A]})
    assert first.status_code == 200
    r = c.post("/read/v1/page", json={"url": B}, headers={"PAYMENT-SIGNATURE": signed.header})
    assert r.status_code == 402 and "Paying per call" not in r.text


def test_the_single_page_sale_is_unchanged_next_to_the_batch(tmp_path):
    fac = FakeFacilitator()
    c, _ = _origin(tmp_path, fac)
    (accept,) = decode_payment_required(c.post("/read/v1/page", json={"url": A})).accepts
    assert accept.amount == "2000" and accept.pay_to == PAY_TO
    r, _ = _buy(c, {"url": A}, path="/read/v1/page")
    assert r.status_code == 200 and r.json()["receipt"]["price_usd"] == "0.002"
    assert "pages" not in r.json()


# -- MPP ---------------------------------------------------------------------------------------
class UnitRail:
    """A Tempo rail stand-in that records the units each step was asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.n = 0

    async def challenge_headers(self, url: str, method: str = "POST", units: int = 1):
        self.calls.append(("challenge", units))
        return [("WWW-Authenticate", f'Payment id="x", method="tempo", amount="{units}"')]

    async def validate(self, authorization: str, url: str | None = None, units: int = 1):
        self.calls.append(("validate", units))
        self.n += 1
        return SimpleNamespace(payload={"type": "transaction", "n": self.n}), PAYER, "transaction"

    async def settle(self, credential, units: int = 1):
        self.calls.append(("settle", units))
        return SimpleNamespace(reference="0x" + "b" * 64, payer=PAYER,
                               to_payment_receipt=lambda: "receipt")  # fmt: skip


PAY = {"Authorization": "Payment eyJjcmVkZW50aWFsIjoieCJ9"}


def test_mpp_offers_validates_and_settles_the_batch_amount(tmp_path):
    rail = UnitRail()
    c, _ = _origin(tmp_path, FakeFacilitator(), rail)
    assert c.post("/read/v1/pages", json={"urls": [A, B, C]}).status_code == 402
    r = c.post("/read/v1/pages", json={"urls": [A, B, C]}, headers=PAY)
    assert r.status_code == 200 and r.json()["receipt"]["price_usd"] == "0.006"
    assert ("challenge", 3) in rail.calls and ("validate", 3) in rail.calls
    assert ("settle", 3) in rail.calls


def test_mpp_never_broadcasts_an_incomplete_batch(tmp_path):
    rail = UnitRail()
    c, _ = _origin(tmp_path, FakeFacilitator(), rail)
    r = c.post("/read/v1/pages", json={"urls": [A, "https://news.test/missing"]}, headers=PAY)
    assert r.status_code == 422 and r.json()["charged"] is False
    assert not any(step == "settle" for step, _ in rail.calls)
    assert "Paying per call" not in r.text


def _real_rail():
    cfg = rail_mod.MppSettings(chain_id=rail_mod.TESTNET, secret_key="shared-secret", realm="r")
    return rail_mod.TempoRail(cfg, PAY_TO, "0.002")


async def _credential(rail, url: str, units: int = 1) -> str:
    from mpp import Challenge, Credential

    header = (await rail.challenge_headers(url, "POST", units))[0][1]
    echo = Challenge.from_www_authenticate(header).to_echo()
    return Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "c" * 64}
                      ).to_authorization()  # fmt: skip


def test_an_mpp_challenge_states_the_batch_amount():
    from mpp import Challenge

    rail = _real_rail()
    header = asyncio.run(rail.challenge_headers("https://h/read/v1/pages", "POST", 3))[0][1]
    request = Challenge.from_www_authenticate(header).request
    assert request["_mppx_scope"]["resource"] == "/read/v1/pages"
    assert int(request["amount"]) == 6000


def test_a_single_page_mpp_credential_is_refused_on_the_batch_route():
    rail = _real_rail()
    auth = asyncio.run(_credential(rail, "https://h/read/v1/page"))
    assert asyncio.run(rail.validate(auth, "https://h/read/v1/pages", 1)) == "RouteMismatch"


def test_an_mpp_credential_for_n_pages_is_refused_for_another_n(monkeypatch):
    rail = _real_rail()
    seen = []

    async def validate_credential(authorization, **k):
        seen.append(k["request"]["amount"])
        raise ValueError("amount mismatch")  # what the SDK does when the HMAC'd amount differs

    monkeypatch.setattr(rail.server, "validate_credential", validate_credential)
    auth = asyncio.run(_credential(rail, "https://h/read/v1/pages", 2))
    assert asyncio.run(rail.validate(auth, "https://h/read/v1/pages", 3)) == "ValueError"
    assert seen == ["0.006"]  # validated against 3 pages' amount, not the credential's own


def test_the_real_sdk_refuses_a_credential_for_another_amount():
    from mpp import Challenge, Credential

    rail = _real_rail()
    header = asyncio.run(rail.challenge_headers("https://h/read/v1/pages", "POST", 2))[0][1]
    echo = Challenge.from_www_authenticate(header).to_echo()
    auth = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "d" * 64}
                      ).to_authorization()  # fmt: skip
    # The challenge's HMAC covers its amount: a 2-page challenge is invalid for 3 pages, and
    # the SDK says so before touching the chain.
    assert asyncio.run(rail.validate(auth, "https://h/read/v1/pages", 3)) == "InvalidChallengeError"


# -- on the shared origin ----------------------------------------------------------------------
def test_off_by_default_and_listed_when_enabled(tmp_path):
    from test_web_search import ROOT, _load

    cohost = _load("budget_gateway_cohost_batch", ROOT / "hosted" / "budget_gateway" / "cohost.py")
    trial_mod, hosted_mod = cohost.load_modules()
    hosted = hosted_mod.Settings.from_env(
        {"BG_DATA_DIR": str(tmp_path / "hosted"), "BG_MOUNT_PREFIX": "/hosted",
         "BG_PUBLIC_BASE_URL": "http://testserver/hosted", "BG_X402_PAY_TO": "0x" + "9" * 40}
    )  # fmt: skip
    env = {"READ_PAY_TO": PAY_TO, "READ_DATA_DIR": str(tmp_path / "wr"),
           "READ_BATCH_DATA_DIR": str(tmp_path / "wrb")}  # fmt: skip
    assert cohost.web_read_batch_from_env(hosted, env) is None  # READ_BATCH unset
    mounted = cohost.web_read_batch_from_env(hosted, {**env, "READ_BATCH": "1"})
    assert mounted is not None and mounted[1].pay_to == PAY_TO
    assert mounted[1].resource_url == "http://testserver/read/v1/pages"
    assert mounted[1].data_dir == tmp_path / "wrb"
    paths = cohost._web_read_batch_paths(mounted[1])
    price = paths["/read/v1/pages"]["post"]["x-payment-info"]["price"]
    assert price == {"mode": "dynamic", "currency": "USD", "min": "0.002", "max": "0.02"}


# -- the single-page protections hold inside a batch -------------------------------------------
@pytest.mark.parametrize(
    ("url", "code"),
    [("http://internal.test/admin", "non_public_address"),
     ("http://localhost/", "non_public_address"),
     ("file:///etc/passwd", "unsupported_url"),
     ("https://news.test:8443/", "unsupported_url")],
)  # fmt: skip
def test_private_and_unsupported_addresses_fail_the_batch_free(tmp_path, url, code):
    fac = FakeFacilitator()
    c, seen = _origin(tmp_path, fac)
    r, _ = _buy(c, {"urls": [A, url]})
    assert r.status_code == 422 and fac.calls_to("settle") == []
    assert r.json()["results"][1] == {"url": url, "delivered": False, "error": code,
                                      "detail": r.json()["results"][1]["detail"]}  # fmt: skip
    assert not any(k.startswith(("internal.test", "localhost")) for k in seen)


def test_robots_txt_is_respected_in_a_batch(tmp_path):
    fac = FakeFacilitator()
    seen: list[str] = []
    base = _site(seen)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "closed.test":
            seen.append(f"closed.test{request.url.path}")
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
            return httpx.Response(200, headers=HTML, text=ARTICLE)
        return base.handle_request(request)

    reader = fetch.Fetcher("test", transport=httpx.MockTransport(handler), resolve=_public_dns)
    settings = svc.Settings(pay_to=PAY_TO, resource_url="http://testserver/read/v1/pages",
                            data_dir=tmp_path, mount_prefix="")  # fmt: skip
    app = svc.create_app(settings, facilitator=fac, task=batch.batch_task(svc, reader))
    c = TestClient(Starlette(routes=[Route("/read/v1/pages", endpoint=app, methods=["POST"])]))
    r, _ = _buy(c, {"urls": [A, "https://closed.test/page"]})
    assert r.status_code == 422 and r.json()["results"][1]["error"] == "disallowed_by_robots_txt"
    assert "closed.test/page" not in seen and fac.calls_to("settle") == []


def test_a_batch_over_the_size_limit_is_refused_free(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "MAX_TOTAL_CHARS", 50)
    fac = FakeFacilitator()
    c, _ = _origin(tmp_path, fac)
    r, _ = _buy(c, {"urls": [A, B]})
    assert r.status_code == 422 and r.json()["error"] == "too_large"
    assert fac.calls_to("settle") == [] and "Paying per call" not in r.text


def test_a_batch_that_runs_out_of_time_is_refused_free(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "DEADLINE_S", 0.05)

    async def slow(self, url):  # every page takes longer than the batch's deadline
        await asyncio.sleep(1)

    monkeypatch.setattr(fetch.Fetcher, "fetch", slow)
    fac = FakeFacilitator()
    c, _ = _origin(tmp_path, fac)
    r, _ = _buy(c, {"urls": [A, B]})
    assert r.status_code == 504 and r.json()["charged"] is False
    assert fac.calls_to("settle") == []


def test_at_most_four_pages_are_fetched_at_once(tmp_path, monkeypatch):
    live, peak = 0, 0
    original = fetch.Fetcher.fetch

    async def counted(self, url):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        try:
            return await original(self, url)
        finally:
            live -= 1

    monkeypatch.setattr(fetch.Fetcher, "fetch", counted)
    c, _ = _origin(tmp_path, FakeFacilitator())
    urls = [A, B, C] + [f"https://news.test/a?{i}" for i in range(7)]
    r, _ = _buy(c, {"urls": urls})
    assert r.status_code == 200 and r.json()["count"] == 10
    assert peak == batch.CONCURRENCY == 4


def test_the_origin_guide_mentions_batching_only_when_it_is_on(tmp_path, monkeypatch):
    from test_web_search import ROOT, _load

    cohost = _load("budget_gateway_cohost_batch2", ROOT / "hosted" / "budget_gateway" / "cohost.py")
    trial_mod, hosted_mod = cohost.load_modules()
    hosted = hosted_mod.Settings.from_env(
        {"BG_DATA_DIR": str(tmp_path / "hosted"), "BG_MOUNT_PREFIX": "/hosted",
         "BG_PUBLIC_BASE_URL": "http://testserver/hosted", "BG_X402_PAY_TO": "0x" + "9" * 40}
    )  # fmt: skip
    for enabled in (False, True):
        monkeypatch.setenv("READ_PAY_TO", PAY_TO)
        monkeypatch.setenv("READ_DATA_DIR", str(tmp_path / f"wr{enabled}"))
        monkeypatch.setenv("READ_BATCH_DATA_DIR", str(tmp_path / f"wrb{enabled}"))
        if enabled:
            monkeypatch.setenv("READ_BATCH", "1")
        else:
            monkeypatch.delenv("READ_BATCH", raising=False)
        app = cohost.create_combined_app(
            tmp_path / f"trial{enabled}", hosted, facilitator=FakeFacilitator(),
            modules=(trial_mod, hosted_mod), web_search=None, finder=None, packages=None,
            domains=None, godmode=None,
        )  # fmt: skip
        with TestClient(app) as tc:
            guide = tc.get("/llms.txt").text
            paths = tc.get("/openapi.json").json()["paths"]
            assert ("/read/v1/pages" in guide) is enabled
            assert ("/read/v1/pages" in paths) is enabled
            assert "/read/v1/page" in paths
            r = tc.post("/read/v1/pages", json={"urls": [A]})
            assert r.status_code == (402 if enabled else 404)
