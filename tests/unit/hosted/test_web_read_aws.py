"""Web Read sold through AWS Marketplace Agentic Commerce (aws_rail.py; off unless configured).

These run the real AWS seller SDK (`fiat-extension-for-x402`) and x402 middleware end to end. The
AWS payment service is stood in for by a locally generated key whose JWKS the verifier fetches
through a mock transport, so the payment tokens are signed and checked exactly as in production.
They prove that:

- the USDC route and its offer are unchanged, and the AWS route is only mounted when configured;
- an unpaid request for a page that can't be read gets its free refusal and **no quote**, so an
  AWS payment (captured when the token is issued) can never be made for nothing;
- a readable page gets a signed quote for $0.002 on `aws:base` bound to the product ID, and the
  paid retry is served the page held from the unpaid read (one fetch in total);
- a payment is used once: replaying the same token returns the stored result, never a second
  delivery; forged, expired and other-quote tokens are refused;
- a paid request that can't be delivered is recorded as owed back (`refund_due`), and says so;
- a bad AWS configuration leaves the AWS rail off without taking Web Read down.
"""

from __future__ import annotations

import importlib.util
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("x402")
pytest.importorskip("fiat_extension_x402")

import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fiat_extension_x402 import (  # noqa: E402
    AgenticCommerceVerifier,
    LocalQuoteSigner,
    StaticSellerKeyResolver,
)
from jwt.algorithms import ECAlgorithm  # noqa: E402
from x402.http.utils import (  # noqa: E402
    decode_payment_required_header,
    encode_payment_signature_header,
)
from x402.schemas import (  # noqa: E402
    PaymentPayload,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
)
from x402.schemas.responses import SettleResponse  # noqa: E402

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

PAY_TO = "0x" + "7" * 40
PRODUCT = "prod-abc123def456"
ISSUER = "awsmp-agentpay.amazonaws.com"
SERVICE_JWKS = "https://payments.test"
ARTICLE = (
    "<html><head><title>T</title></head>"
    "<body><main><h1>Hello</h1><p>World.</p></main></body></html>"
)
HTML = {"content-type": "text/html; charset=utf-8"}


def _pem() -> bytes:
    key = ec.generate_private_key(ec.SECP384R1())
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


SELLER_PEM = _pem()
SERVICE_KEY = ec.generate_private_key(ec.SECP256R1())
SERVICE_JWK = ECAlgorithm.to_jwk(SERVICE_KEY.public_key(), as_dict=True) | {
    "alg": "ES256",
    "kid": "svc-1",
    "use": "sig",
}
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


@dataclass
class Site:
    """The pages the reader sees; `hits` counts fetches of each path."""

    pages: dict[str, httpx.Response] = field(default_factory=lambda: {
        "news.test/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
        "news.test/a": httpx.Response(200, text=ARTICLE, headers=HTML),
    })  # fmt: skip
    hits: dict[str, int] = field(default_factory=dict)

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            key = f"{request.url.host}{request.url.path}"
            self.hits[key] = self.hits.get(key, 0) + 1
            r = self.pages.get(key)
            return (
                httpx.Response(404)
                if r is None
                else httpx.Response(r.status_code, headers=r.headers, content=r.content)
            )

        return httpx.MockTransport(handler)


class BaseFacilitator:
    """The USDC rail's facilitator; the AWS tests never pay through it."""

    def get_supported(self) -> SupportedResponse:
        return SupportedResponse(
            kinds=[SupportedKind(x402_version=2, scheme="exact", network=svc.TESTNET)]
        )

    async def verify(self, payload: Any, requirements: Any) -> VerifyResponse:
        raise AssertionError("the USDC facilitator must not see AWS payments")

    async def settle(self, payload: Any, requirements: Any) -> SettleResponse:
        raise AssertionError("the USDC facilitator must not see AWS payments")


async def _public_dns(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "READ_PAY_TO": PAY_TO,
        "READ_RESOURCE_URL": "http://testserver/v1/page",
        "READ_DATA_DIR": str(tmp_path),
        "READ_AWS_PRODUCT_ID": PRODUCT,
        "READ_AWS_APPROVED": "1",
        "READ_AWS_QUOTE_KEY_PEM": SELLER_PEM.decode(),
        "READ_AWS_KEY_ID": "quote-test-1",
        **extra,
    }


def _verifier() -> AgenticCommerceVerifier:
    seller = LocalQuoteSigner.from_pem(
        SELLER_PEM, kid="quote-test-1", service_url="http://testserver/aws"
    )
    v = AgenticCommerceVerifier(
        seller_key_resolver=StaticSellerKeyResolver(seller.public_jwk),
        payment_token_jwks_url=SERVICE_JWKS,
    )
    jwks = httpx.MockTransport(lambda r: httpx.Response(200, json={"keys": [SERVICE_JWK]}))
    v._http = httpx.Client(transport=jwks)  # the stand-in AWS payment service's JWKS
    return v


def _client(tmp_path: Path, site: Site, **env: str) -> TestClient:
    settings = svc.Settings.from_env(_env(tmp_path, **env))
    reader = fetch.Fetcher(settings.user_agent, transport=site.transport(), resolve=_public_dns)
    app = svc.create_app(
        settings, facilitator=BaseFacilitator(), fetcher=reader, aws_verifier=_verifier()
    )
    return TestClient(app)


def _quote(response: httpx.Response) -> tuple[Any, Any, dict[str, Any]]:
    assert response.status_code == 402, response.text
    required = decode_payment_required_header(response.headers["payment-required"])
    accept = required.accepts[0]
    claims = jwt.decode(accept.extra["quoteToken"], options={"verify_signature": False})
    return required, accept, claims


def _pay(required: Any, accept: Any, claims: dict[str, Any], *, key: Any = SERVICE_KEY,
         payment_id: str | None = None, exp: int | None = None,
         quote: str | None = None) -> str:  # fmt: skip
    """What AWS Marketplace hands the buyer's agent: a payment token for this quote."""
    token = jwt.encode(
        {"iss": ISSUER, "aud": claims["iss"], "sub": "buyer-acct-1",
         "payment_id": payment_id or uuid.uuid4().hex,
         "quote_token": quote or accept.extra["quoteToken"], "exp": exp or int(time.time()) + 300},
        key, algorithm="ES256", headers={"kid": "svc-1"},
    )  # fmt: skip
    payload = PaymentPayload(x402_version=2, accepted=accept, payload={"paymentToken": token},
                             resource=required.resource)  # fmt: skip
    return encode_payment_signature_header(payload)


def test_usdc_route_is_unchanged_and_has_no_aws_offer(tmp_path):
    c = _client(tmp_path, Site())
    r = c.post("/v1/page", json={"url": "https://news.test/a"})
    required = decode_payment_required_header(r.headers["payment-required"])
    assert r.status_code == 402
    assert [a.network for a in required.accepts] == [svc.TESTNET]
    assert required.accepts[0].pay_to == PAY_TO


def test_aws_route_absent_unless_configured(tmp_path):
    env = _env(tmp_path)
    del env["READ_AWS_PRODUCT_ID"]
    settings = svc.Settings.from_env(env)
    assert settings.aws is None
    app = svc.create_app(
        settings, facilitator=BaseFacilitator(), fetcher=fetch.Fetcher(settings.user_agent)
    )
    assert (
        TestClient(app).post("/aws/v1/page", json={"url": "https://news.test/a"}).status_code == 404
    )


@pytest.mark.parametrize("bad", [{"READ_AWS_APPROVED": "0"},
                                 {"READ_AWS_PRODUCT_ID": "not-a-product"},
                                 {"READ_AWS_QUOTE_KEY_PEM": "nope"}])  # fmt: skip
def test_bad_aws_config_leaves_only_the_aws_rail_off(tmp_path, bad):
    settings = svc.Settings.from_env(_env(tmp_path, **bad))
    assert settings.aws is None and settings.pay_to == PAY_TO  # Web Read itself still configured


def test_readable_page_gets_a_signed_quote_bound_to_the_product(tmp_path):
    c = _client(tmp_path, Site())
    required, accept, claims = _quote(c.post("/aws/v1/page", json={"url": "https://news.test/a"}))
    assert (accept.scheme, accept.network, accept.amount, accept.asset) == (
        "exact",
        "aws:base",
        "0.002",
        "iso4217:USD",
    )
    assert claims["settlement"]["product_id"] == PRODUCT
    assert "aws:marketplace" in (
        claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    )
    assert claims["iss"] == "http://testserver/aws"
    assert claims["price"] == {"amount": "0.002", "currency": "USD"}


def test_unreadable_page_is_refused_free_with_no_quote(tmp_path):
    c = _client(tmp_path, Site())
    r = c.post("/aws/v1/page", json={"url": "https://news.test/missing"})
    assert r.status_code in (422, 502) and r.json()["charged"] is False
    assert "payment-required" not in r.headers


def test_bare_probe_gets_the_quote(tmp_path):
    c = _client(tmp_path, Site())
    _quote(c.post("/aws/v1/page"))


def test_paid_request_is_served_from_the_unpaid_read(tmp_path):
    site = Site()
    c = _client(tmp_path, site)
    body = {"url": "https://news.test/a", "request_id": "job-1"}
    required, accept, claims = _quote(c.post("/aws/v1/page", json=body))
    r = c.post(
        "/aws/v1/page",
        json=body,
        headers={"payment-signature": _pay(required, accept, claims, payment_id="p-1")},
    )
    assert r.status_code == 200, r.text
    got = r.json()
    assert "Hello" in got["content_markdown"]
    assert got["receipt"] == {
        "payment_ref": "aws:p-1",
        "price_usd": "0.002",
        "request_id": "job-1",
        "rail": "aws",
    }
    assert site.hits["news.test/a"] == 1  # read once, for the unpaid request


def test_a_payment_is_used_once_and_a_replay_gets_the_stored_result(tmp_path):
    site = Site()
    c = _client(tmp_path, site)
    body = {"url": "https://news.test/a"}
    required, accept, claims = _quote(c.post("/aws/v1/page", json=body))
    header = _pay(required, accept, claims, payment_id="p-2")
    first = c.post("/aws/v1/page", json=body, headers={"payment-signature": header})
    again = c.post("/aws/v1/page", json=body, headers={"payment-signature": header})
    assert first.status_code == again.status_code == 200
    assert again.headers.get("x-inferrail-recovered") == "1"
    assert again.json()["content_sha256"] == first.json()["content_sha256"]
    assert site.hits["news.test/a"] == 1  # no second delivery for the same payment
    store = next(r.app for r in c.app.routes if getattr(r, "path", "") == "/aws").state.aws_store
    assert store.result("p-2")[0] == "delivered"


@pytest.mark.parametrize("forge", ["wrong_key", "expired", "other_quote"])
def test_forged_or_stale_tokens_are_refused(tmp_path, forge):
    c = _client(tmp_path, Site())
    body = {"url": "https://news.test/a"}
    required, accept, claims = _quote(c.post("/aws/v1/page", json=body))
    kw: dict[str, Any] = {
        "wrong_key": {"key": OTHER_KEY},
        "expired": {"exp": int(time.time()) - 10},
        "other_quote": {"quote": accept.extra["quoteToken"][:-4] + "AAAA"},
    }[forge]
    r = c.post(
        "/aws/v1/page",
        json=body,
        headers={"payment-signature": _pay(required, accept, claims, **kw)},
    )
    assert r.status_code == 402
    assert "content_markdown" not in r.text


def test_paid_but_undeliverable_is_recorded_as_owed_back(tmp_path):
    site = Site()
    c = _client(tmp_path, site)
    body = {"url": "https://news.test/a"}
    required, accept, claims = _quote(c.post("/aws/v1/page", json=body))
    # The hold is lost (e.g. a restart) and the page has gone away since the quote.
    aws_app = next(r.app for r in c.app.routes if getattr(r, "path", "") == "/aws")
    site.pages.pop("news.test/a")
    header = _pay(required, accept, claims, payment_id="p-3")
    # A different request_id has no held page, so the paid request reads the page again.
    r = c.post("/aws/v1/page", json={"url": "https://news.test/a", "request_id": "other"},
               headers={"payment-signature": header})  # fmt: skip
    assert r.status_code == 502
    assert r.json()["refund_due"] is True and r.json()["charged"] is True
    assert aws_app.state.aws_store.result("p-3")[0] == "refund_due"


def test_jwks_publishes_only_the_public_key(tmp_path):
    c = _client(tmp_path, Site())
    keys = c.get("/aws/.well-known/jwks.json").json()["keys"]
    assert len(keys) == 1 and keys[0]["kid"] == "quote-test-1"
    assert "d" not in keys[0]  # never the private part


def test_an_aws_rail_that_fails_to_start_leaves_web_read_serving(tmp_path, monkeypatch):
    aws_rail = sys.modules["web_read_aws_rail"]

    def broken(**_: Any) -> Any:
        raise ImportError("fiat_extension_x402 is not installed")

    monkeypatch.setattr(aws_rail, "build", broken)
    c = _client(tmp_path, Site())
    assert c.post("/v1/page", json={"url": "https://news.test/a"}).status_code == 402  # USDC offer
    assert c.post("/aws/v1/page", json={"url": "https://news.test/a"}).status_code == 404
