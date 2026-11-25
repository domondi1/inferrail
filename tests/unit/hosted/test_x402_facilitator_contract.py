"""Every paid route's x402 payment makes us send the CDP facilitator a request its published
schema accepts.

Incident 2026-11-23: a 501-character offer description (the facilitator's schema allows 500 in
`resource.description`) got every Web Read payment refused at verify with HTTP 400, while the
offer itself passed Coinbase's public offer validator. These tests capture the real verify and
settle requests the server sends (real x402 SDK, throwaway signer, mocked transport) and check
them against the facilitator's own OpenAPI models from cdp-sdk, for Web Read, its batch, Package
Check, Domain Check and the Finder; and they check that `x402_contract`, which the pre-deploy
check uses on the deploy's own offers, sees exactly what the facilitator would be sent."""

from __future__ import annotations

import copy
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("x402")
pytest.importorskip("eth_account")
pytest.importorskip("cdp")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _x402_fake_facilitator import decode_payment_required  # noqa: E402
from eth_account import Account  # noqa: E402
from test_web_read import ARTICLE, HTML, ROBOTS_OK, _public_dns, fetch, svc  # noqa: E402

contract = svc._sibling("x402_contract")
HERE = Path(__file__).resolve().parent
KNOWN_GOOD = json.loads((HERE / "data_x402_known_good_echo.json").read_text())
MAINNET = "eip155:8453"
ORIGIN = "https://inferrail-cost-gateway.onrender.com"
# The description that went out on 2026-11-22 (501 characters) and got every payment refused.
INCIDENT_DESCRIPTION = (
    "Read one public web page as clean markdown (title, main text, headings, lists, links), "
    "with the page's own metadata (description, canonical URL, authors, published date, "
    "language), word count, a content hash and a receipt. Pay only for a delivered page: a "
    "blocked, missing or failing page (target-site 403, 404, 5xx, timeout, bot check, no text "
    "without JavaScript) costs nothing, and neither do refusals (robots.txt, non-public address, "
    "non-text, >2 MB). A retry from the same wallet never pays twice."
)


def _facilitator(calls: dict, *, valid: bool):
    """The real HTTP facilitator client with its transport mocked: records each request body by
    endpoint and answers verify (valid or a CDP-style schema refusal) and settle."""
    from x402.http import HTTPFacilitatorClient
    from x402.schemas import SupportedKind, SupportedResponse

    class Recording(HTTPFacilitatorClient):
        def get_supported(self):
            return SupportedResponse(kinds=[SupportedKind(x402_version=2, scheme="exact",
                                                          network=MAINNET)],
                                     extensions=[], signers={})  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        endpoint = request.url.path.rsplit("/", 1)[-1]
        calls[endpoint] = body
        payer = body["paymentPayload"]["payload"]["authorization"]["from"]
        if endpoint == "verify":
            if not valid:
                return httpx.Response(400, json={"correlationId": "t", "errorType": "x"})
            return httpx.Response(200, json={"isValid": True, "payer": payer})
        return httpx.Response(200, json={"success": True, "transaction": "0x" + "ab" * 32,
                                         "network": MAINNET, "payer": payer})  # fmt: skip

    fac = Recording()
    fac._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fac._owns_client = False
    return fac


def _pay(app: TestClient, path: str, body: dict) -> tuple[dict, int]:
    """Pay `path` once with a throwaway mainnet signer: (the 402's requirements, status)."""
    from x402 import x402ClientSync
    from x402.http.utils import encode_payment_signature_header
    from x402.mechanisms.evm.exact import ExactEvmScheme

    r = app.post(path, json=body)
    assert r.status_code == 402, r.text
    required = decode_payment_required(r)
    buyer = x402ClientSync()
    buyer.register(MAINNET, ExactEvmScheme(signer=Account.create()))
    header = encode_payment_signature_header(buyer.create_payment_payload(required))
    paid = app.post(path, json=body, headers={"PAYMENT-SIGNATURE": header})
    return contract.decode_payment_required(r.headers["payment-required"]), paid.status_code


def _routes(tmp_path: Path, fac):
    """(name, app, path, body) for every paid task, each as its 402 is built on mainnet."""
    reader = fetch.Fetcher("t", transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                           resolve=_public_dns)  # fmt: skip
    secret = b"s" * 32
    batch, packages = svc._sibling("batch"), svc._sibling("packages")
    domains, finder = svc._sibling("domains"), svc._sibling("finder")

    def app(task, prefix, path, data, pay_to):
        st = svc.Settings(pay_to=pay_to, network=MAINNET, resource_url=f"{ORIGIN}{prefix}{path}",
                          data_dir=tmp_path / data, mount_prefix=prefix)  # fmt: skip
        return TestClient(svc.create_app(st, facilitator=fac, task=task, fetcher=reader))

    yield "web read", app(svc.page_task(reader), "/read", "/v1/page", "r", "0x" + "7" * 40), \
        "/read/v1/page", {"url": "https://example.com/"}
    yield "batch", app(batch.batch_task(svc, reader), "", "/read/v1/pages", "b", "0x" + "7" * 40), \
        "/read/v1/pages", {"urls": ["https://example.com/", "https://example.org/"]}
    yield "package check", app(packages.package_task(svc, secret), "/pkg", "/v1/check", "p",
                               "0x" + "8" * 40), "/pkg/v1/check", {
        "packages": [{"ecosystem": "npm", "name": "lodash", "version": "4.17.15"}]}
    yield "domain check", app(domains.domain_task(svc, secret), "/domain", "/v1/lookup", "d",
                              "0x" + "9" * 40), "/domain/v1/lookup", {"domain": "example.com"}
    yield "finder", app(finder.finder_task(svc, finder.Catalog(), secret), "/find", "/v1/find",
                        "f", "0x" + "6" * 40), "/find/v1/find", {"need": "read a web page"}


def test_every_routes_real_verify_request_is_accepted_by_the_facilitator_schema(tmp_path):
    calls: dict = {}
    for name, app, path, body in _routes(tmp_path, _facilitator(calls, valid=False)):
        calls.clear()
        _pay(app, path, body)
        assert "verify" in calls, name
        assert contract.body_problems(calls["verify"]) == [], name


def test_the_offer_check_sees_exactly_what_the_facilitator_is_sent(tmp_path):
    """The pre-deploy check builds the verify body from the 402 alone; every echoed field must
    be the one the real SDK sends, or a passing check would prove nothing."""
    calls: dict = {}
    for name, app, path, body in _routes(tmp_path, _facilitator(calls, valid=False)):
        calls.clear()
        required, _ = _pay(app, path, body)
        sent = calls["verify"]
        built = contract.verify_body(required, required["accepts"][0])
        assert built["paymentRequirements"] == sent["paymentRequirements"], name
        for field in ("resource", "accepted", "extensions", "x402Version"):
            assert built["paymentPayload"].get(field) == sent["paymentPayload"].get(field), (
                name, field)
        assert contract.offer_problems(required) == [], name


def test_a_delivered_sale_settles_with_a_request_the_facilitator_schema_accepts(tmp_path):
    calls: dict = {}
    settings = svc.Settings(pay_to="0xA45140E99E9482a50BC3fA5F6000c02FAAB6F65E", network=MAINNET,
                            resource_url=f"{ORIGIN}/read/v1/page", data_dir=tmp_path,
                            mount_prefix="/read")  # fmt: skip
    site = {"news.test/robots.txt": ROBOTS_OK,
            "news.test/a": httpx.Response(200, text=ARTICLE, headers=HTML)}  # fmt: skip
    def page(request: httpx.Request) -> httpx.Response:
        found = site.get(f"{request.url.host}{request.url.path}")
        return found if found is not None else httpx.Response(404)

    reader = fetch.Fetcher(settings.user_agent, resolve=_public_dns,
                           transport=httpx.MockTransport(page))  # fmt: skip
    from fastapi import FastAPI

    host = FastAPI()  # mounted under its prefix, as the shared host serves it
    host.mount("/read", svc.create_app(settings, facilitator=_facilitator(calls, valid=True),
                                       fetcher=reader))  # fmt: skip
    app = TestClient(host)
    _, status = _pay(app, "/read/v1/page", {"url": "https://news.test/a"})
    assert status == 200
    assert contract.body_problems(calls["verify"]) == []
    assert contract.body_problems(calls["settle"]) == []


def _known_good_body() -> dict:
    accepted = {"scheme": "exact", "network": MAINNET,
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "2000",
                "payTo": "0xA45140E99E9482a50BC3fA5F6000c02FAAB6F65E", "maxTimeoutSeconds": 300,
                "extra": {"name": "USD Coin", "version": "2"}}  # fmt: skip
    required = {"x402Version": 2, "accepts": [accepted], **copy.deepcopy(KNOWN_GOOD)}
    return required


def test_the_incident_description_is_refused_and_the_limit_is_the_facilitators():
    required = _known_good_body()
    assert len(INCIDENT_DESCRIPTION) == 501
    assert contract.offer_problems(required) == []
    required["resource"]["description"] = INCIDENT_DESCRIPTION
    found = contract.offer_problems(required)
    assert len(found) == 1 and "at most 500 characters" in found[0]
    required["resource"]["description"] = INCIDENT_DESCRIPTION[:500]
    assert contract.offer_problems(required) == []
    assert contract.description_limit() == 500


def test_our_description_ceiling_is_inside_the_facilitators():
    assert svc.OFFER_DESCRIPTION_MAX <= contract.description_limit()


def test_options_the_check_does_not_cover_are_reported_not_passed():
    required = _known_good_body()
    required["accepts"][0]["scheme"] = "upto"
    assert "no facilitator contract check" in contract.offer_problems(required)[0]
    assert contract.offer_problems({"x402Version": 2, "accepts": []}) == [
        "the offer has no payment options"]


def _preflight():
    import importlib.util

    hosted = Path(__file__).resolve().parents[3] / "hosted" / "budget_gateway"
    sys.path.insert(0, str(hosted))
    spec = importlib.util.spec_from_file_location("hosted_preflight_contract",
                                                  hosted / "preflight.py")  # fmt: skip
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import cohost

    return module, cohost


def test_preflight_passes_the_live_shaped_batch_offer(tmp_path, capsys):
    preflight, cohost = _preflight()
    env = {"BG_DATA_DIR": str(tmp_path / "data"), "READ_PAY_TO": "0x" + "5" * 40,
           "READ_BATCH": "1"}  # fmt: skip
    settings = cohost.load_modules()[1].Settings.from_env({**env, "BG_MOUNT_PREFIX": cohost.PREFIX})
    assert preflight.check_offers(cohost, settings, env) == []
    out = capsys.readouterr().out
    assert "web read: offer passes" in out and "web read batch: offer passes" in out


def test_preflight_stops_a_deploy_whose_offer_the_facilitator_would_refuse(tmp_path):
    preflight, cohost = _preflight()
    settings = svc.Settings(pay_to="0x" + "7" * 40, network=MAINNET,
                            resource_url=f"{ORIGIN}/read/v1/page", data_dir=tmp_path,
                            mount_prefix="/read")  # fmt: skip
    reader = fetch.Fetcher("t", resolve=_public_dns)
    import dataclasses

    long_task = dataclasses.replace(svc.page_task(reader), description=INCIDENT_DESCRIPTION)
    fake = types.SimpleNamespace(
        _load=cohost._load, HOSTED_DIR=cohost.HOSTED_DIR,
        web_read_from_env=lambda s, e: (
            svc.create_app(settings, task=long_task, facilitator=_facilitator({}, valid=False)),
            settings),
        web_read_batch_from_env=None, finder_from_env=None, package_check_from_env=None,
        domain_check_from_env=None,
    )  # fmt: skip
    found = preflight.check_offers(fake, None, {"READ_PAY_TO": settings.pay_to})
    assert len(found) == 1
    assert found[0].startswith("web read: the facilitator would refuse every payment")
    assert "at most 500 characters" in found[0]


def test_an_unpaid_402_tells_a_non_x402_caller_how_to_pay_without_confusing_x402_clients(tmp_path):
    """Every route's unpaid 402 carries a readable guide (the SDK default body is `{}`). It has
    no `x402Version`, so a real x402 client still takes the offer from the header, and the price
    it states is the header's."""
    from x402 import x402ClientSync
    from x402.http import x402HTTPClientSync

    reader = x402HTTPClientSync(x402ClientSync())
    for name, app, path, body in _routes(tmp_path, _facilitator({}, valid=False)):
        r = app.post(path, json=body)
        guide = r.json()
        assert r.status_code == 402 and "x402Version" not in guide, name
        assert guide["charged"] is False and guide["charged_only_on_delivery"] is True, name
        assert guide["start_paying"]["vercel_ai_sdk"] == "npm i @inferrail/ai-sdk-tools", name
        assert guide["docs"]["guide"].endswith("/llms.txt"), name
        offer = reader.get_payment_required_response(r.headers.get, r.content)
        header = contract.decode_payment_required(r.headers["payment-required"])
        assert offer.model_dump(by_alias=True, exclude_none=True)["accepts"][0]["amount"] == \
            header["accepts"][0]["amount"], name
        assert str(int(header["accepts"][0]["amount"]) / 1_000_000) == \
            str(float(guide["price_usd"])), name
