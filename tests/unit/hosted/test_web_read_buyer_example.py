"""examples/web_read/buy_page.py against a real Web Read app (fake facilitator, no funds).

Proves the published buyer example works as documented: it pays the 402 and gets the page with
a receipt, refuses an offer above --max-usd before signing anything, and a retry with the same
request_id after a lost response is answered without a second charge.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("x402")
pytest.importorskip("eth_account")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _x402_fake_facilitator import FakeFacilitator  # noqa: E402
from eth_account import Account  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


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
fetch = sys.modules["web_read_fetch"]
example = _load("web_read_buy_page", ROOT / "examples" / "web_read" / "buy_page.py")

PAY_TO = "0x" + "7" * 40
ENDPOINT = "http://testserver/v1/page"
ARTICLE = "<html><head><title>T</title></head><body><main><h1>Hello</h1></main></body></html>"
SITE = {
    "news.test/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
    "news.test/a": httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"}),
}


def _site() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        r = SITE.get(f"{request.url.host}{request.url.path}")
        return (
            httpx.Response(404)
            if r is None
            else httpx.Response(r.status_code, headers=r.headers, content=r.content)
        )

    return httpx.MockTransport(handler)


async def _dns(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def _app(tmp_path: Path, fac: FakeFacilitator, price: str = "0.002"):
    settings = svc.Settings(
        pay_to=PAY_TO, resource_url=ENDPOINT, data_dir=tmp_path, price_usd=price,
        operator_token="op-token",
    )  # fmt: skip
    reader = fetch.Fetcher(settings.user_agent, transport=_site(), resolve=_dns)
    return svc.create_app(settings, facilitator=fac, fetcher=reader)


def _buy(app, **kw):
    return asyncio.run(
        example.buy_page(
            "https://news.test/a",
            Account.create(),
            endpoint=ENDPOINT,
            network=svc.TESTNET,
            transport=httpx.ASGITransport(app=app),
            **kw,
        )  # fmt: skip
    )


def test_it_pays_and_gets_the_page_with_a_receipt(tmp_path):
    fac = FakeFacilitator()
    page = _buy(_app(tmp_path, fac), request_id="r1")
    assert "# Hello" in page["content_markdown"]
    assert page["receipt"]["price_usd"] == "0.002" and page["receipt"]["request_id"] == "r1"
    assert len(fac.calls_to("settle")) == 1


def test_it_refuses_an_offer_above_its_ceiling_before_signing(tmp_path):
    fac = FakeFacilitator()
    from x402.http.clients.httpx import PaymentError

    with pytest.raises(PaymentError, match="max_amount_per_payment"):  # refused before signing
        _buy(_app(tmp_path, fac, price="0.01"), max_usd="0.002")
    assert fac.calls_to("verify") == [] and fac.calls_to("settle") == []


def test_a_missing_page_is_reported_and_not_charged(tmp_path):
    fac = FakeFacilitator()
    app = _app(tmp_path, fac)
    with pytest.raises(example.PageNotBought) as err:
        asyncio.run(
            example.buy_page(
                "https://news.test/missing",
                Account.create(),
                endpoint=ENDPOINT,
                network=svc.TESTNET,
                transport=httpx.ASGITransport(app=app),
            )  # fmt: skip
        )
    assert err.value.status == 422
    assert fac.calls_to("settle") == []


def test_a_retry_after_a_lost_response_is_not_charged_again(tmp_path):
    fac = FakeFacilitator()
    app = _app(tmp_path, fac)
    account = Account.create()
    inner = httpx.ASGITransport(app=app)
    paid_once = {"n": 0}

    class LoseFirstPaidResponse(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):  # type: ignore[override]
            response = await inner.handle_async_request(request)
            if request.headers.get("payment-signature") and paid_once["n"] == 0:
                paid_once["n"] += 1
                await response.aread()
                raise httpx.ReadError("connection dropped after the page was paid")
            return response

    page = asyncio.run(
        example.buy_page(
            "https://news.test/a",
            account,
            endpoint=ENDPOINT,
            network=svc.TESTNET,
            request_id="same",
            transport=LoseFirstPaidResponse(),
        )  # fmt: skip
    )
    assert "# Hello" in page["content_markdown"]
    assert len(fac.calls_to("settle")) == 1  # the retry was answered from the paid result


def test_the_skill_states_the_live_contract():
    skill = (ROOT / "skills" / "web-read" / "SKILL.md").read_text()
    head = skill.split("---")[1]
    assert "name: inferrail-web-read" in head and "description:" in head
    settings = svc.Settings(pay_to=PAY_TO, resource_url=ENDPOINT)
    assert f"${settings.price_usd}" in skill and "eip155:8453" in skill
    assert "2000 units" in skill and "/read/mcp/" in skill and "read_page" in skill
    assert f"within {settings.recovery_s // 60} minutes" in skill
    assert "examples/web_read/buy_page.py" in skill
