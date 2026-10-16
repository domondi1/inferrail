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
    sign_payment,
)

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
