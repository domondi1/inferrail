"""What our offers make buyers send to the CDP facilitator.

Incident 2026-11-23: after #223, every x402 verify request for Web Read was refused by the CDP
facilitator with HTTP 400 (three funded EOA payers, one of them a 25-time repeat buyer). The
only differences from the requests it had accepted for every settled sale were fields the buyer
echoes from our 402: a longer resource description (501 characters) and a discovery-extension
output schema with `["string", "null"]` types and null example values. These tests keep the
echoed fields in the shape the facilitator accepted (`data_x402_known_good_echo.json` is the
echo from the pre-#223 code that settled those sales)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("x402")
pytest.importorskip("eth_account")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _x402_fake_facilitator import FakeFacilitator, decode_payment_required  # noqa: E402
from eth_account import Account  # noqa: E402
from test_web_read import ARTICLE, HTML, ROBOTS_OK, _public_dns, fetch, svc  # noqa: E402

HERE = Path(__file__).resolve().parent
KNOWN_GOOD = json.loads((HERE / "data_x402_known_good_echo.json").read_text())
MAINNET = "eip155:8453"
READ = "https://inferrail-cost-gateway.onrender.com/read/v1/page"


def _verify_request(task_app: TestClient, path: str, body: dict, captured: dict) -> int:
    """Pay `path` with a throwaway mainnet signer; return the status. `captured` gets the
    facilitator verify request body the server sent."""
    from x402 import x402ClientSync
    from x402.http.utils import encode_payment_signature_header
    from x402.mechanisms.evm.exact import ExactEvmScheme

    required = decode_payment_required(task_app.post(path, json=body))
    buyer = x402ClientSync()
    buyer.register(MAINNET, ExactEvmScheme(signer=Account.create()))
    header = encode_payment_signature_header(buyer.create_payment_payload(required))
    return task_app.post(path, json=body, headers={"PAYMENT-SIGNATURE": header}).status_code


def _capturing_facilitator(captured: dict, answer: httpx.Response):
    from x402.http import HTTPFacilitatorClient
    from x402.schemas import SupportedKind, SupportedResponse

    class Capturing(HTTPFacilitatorClient):
        def get_supported(self):
            return SupportedResponse(kinds=[SupportedKind(x402_version=2, scheme="exact",
                                                          network=MAINNET)],
                                     extensions=[], signers={})  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return answer

    fac = Capturing()
    fac._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fac._owns_client = False
    return fac


def _web_read(tmp_path: Path, fac) -> TestClient:
    settings = svc.Settings(pay_to="0xA45140E99E9482a50BC3fA5F6000c02FAAB6F65E", network=MAINNET,
                            resource_url=READ, data_dir=tmp_path, mount_prefix="/read")  # fmt: skip
    site = {"news.test/robots.txt": ROBOTS_OK,
            "news.test/a": httpx.Response(200, text=ARTICLE, headers=HTML)}  # fmt: skip

    def transport(request: httpx.Request) -> httpx.Response:
        r = site.get(f"{request.url.host}{request.url.path}")
        return r if r is not None else httpx.Response(404)

    reader = fetch.Fetcher(settings.user_agent, transport=httpx.MockTransport(transport),
                           resolve=_public_dns)  # fmt: skip
    return TestClient(svc.create_app(settings, facilitator=fac, fetcher=reader))


def test_web_reads_echoed_fields_are_the_ones_the_facilitator_accepted(tmp_path):
    captured: dict = {}
    fac = _capturing_facilitator(captured, httpx.Response(400, json={"errorType": "x"}))
    _verify_request(_web_read(tmp_path, fac), "/read/v1/page", {"url": "https://news.test/a"},
                    captured)  # fmt: skip
    payload = captured["body"]["paymentPayload"]
    assert payload["resource"] == KNOWN_GOOD["resource"]
    assert payload["extensions"] == KNOWN_GOOD["extensions"]


def _no_type_lists_or_nulls(value, path="") -> list[str]:
    bad = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "type" and isinstance(v, list):
                bad.append(f"{path}/type={v}")
            if v is None and not path.endswith("/input_schema") and k != "default":
                bad.append(f"{path}/{k}=null")
            bad += _no_type_lists_or_nulls(v, f"{path}/{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            bad += _no_type_lists_or_nulls(v, f"{path}[{i}]")
    return bad


def _offer(app: TestClient, path: str, body: dict):
    r = app.post(path, json=body)
    assert r.status_code == 402, r.text
    return decode_payment_required(r)


def _apps(tmp_path: Path):
    """Every paid task, as its 402 is built: Web Read, its batch, Package Check, Domain Check
    and the x402 Finder."""
    from test_web_read import svc as s

    fac = FakeFacilitator()
    batch = s._sibling("batch")
    packages = s._sibling("packages")
    domains = s._sibling("domains")
    finder = s._sibling("finder")
    reader = fetch.Fetcher("t", transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                           resolve=_public_dns)  # fmt: skip

    def app(task, path, data):
        st = s.Settings(pay_to="0x" + "7" * 40, resource_url=f"http://testserver{path}",
                        data_dir=tmp_path / data)  # fmt: skip
        return TestClient(s.create_app(st, facilitator=fac, task=task, fetcher=reader))

    secret = b"s" * 32
    yield "web read", app(s.page_task(reader), "/v1/page", "r"), "/v1/page", {
        "url": "https://example.com/"}
    yield "batch", app(batch.batch_task(s, reader), "/read/v1/pages", "b"), "/read/v1/pages", {
        "urls": ["https://example.com/", "https://example.org/"]}
    yield "package check", app(packages.package_task(s, secret), "/v1/check", "p"), "/v1/check", {
        "packages": [{"ecosystem": "npm", "name": "lodash", "version": "4.17.15"}]}
    yield "domain check", app(domains.domain_task(s, secret), "/v1/lookup", "d"), "/v1/lookup", {
        "domain": "example.com"}
    yield "finder", app(finder.finder_task(s, finder.Catalog(), secret), "/v1/find", "f"), \
        "/v1/find", {"need": "read a web page"}


def test_no_offer_puts_type_lists_or_nulls_in_what_buyers_echo(tmp_path):
    for name, app, path, body in _apps(tmp_path):
        offer = _offer(app, path, body)
        ext = json.loads(json.dumps(offer.model_dump(by_alias=True, exclude_none=True)))
        bad = _no_type_lists_or_nulls(ext.get("extensions", {}).get("bazaar", {}).get("info", {}))
        bad += _no_type_lists_or_nulls(ext.get("extensions", {}).get("bazaar", {}).get("schema", {})
                                       .get("properties", {}).get("output", {}))  # fmt: skip
        assert bad == [], (name, bad[:5])


def test_every_offer_description_is_within_what_the_facilitator_accepted(tmp_path):
    for name, app, path, body in _apps(tmp_path):
        description = _offer(app, path, body).resource.description
        assert 50 <= len(description) <= svc.OFFER_DESCRIPTION_MAX, (name, len(description))


def test_bazaar_safe_keeps_the_first_real_type_and_drops_nulls():
    schema = {"type": "object", "properties": {"a": {"type": ["string", "null"]},
              "b": {"type": ["null", "integer"]}, "c": {"default": None, "type": "string"}}}
    assert svc.bazaar_safe(schema) == {"type": "object", "properties": {
        "a": {"type": "string"}, "b": {"type": "integer"}, "c": {"type": "string"}}}
    assert svc.bazaar_safe({"x": None, "y": [1, None, {"z": None}]}) == {"y": [1, {}]}


@pytest.mark.parametrize(("body", "reason", "message"), [
    ({"correlationId": "a5ee0000-IAD", "errorType": "invalid_request",
      "errorMessage": "paymentPayload.resource.description: too long"},
     "invalid_request", "paymentPayload.resource.description: too long"),
    ({"correlationId": "a5ef0000-IAD", "invalidReason": "insufficient_funds",
      "invalidMessage": "balance 0", "isValid": False}, "insufficient_funds", "balance 0"),
])  # fmt: skip
def test_both_facilitator_error_shapes_become_clean_refusals(tmp_path, body, reason, message):
    captured: dict = {}
    fac = _capturing_facilitator(captured, httpx.Response(400, json=body))
    app = _web_read(tmp_path, fac)
    required = decode_payment_required(app.post("/read/v1/page", json={"url": "https://news.test/a"}))
    from x402 import x402ClientSync
    from x402.http.utils import encode_payment_signature_header
    from x402.mechanisms.evm.exact import ExactEvmScheme

    buyer = x402ClientSync()
    buyer.register(MAINNET, ExactEvmScheme(signer=Account.create()))
    header = encode_payment_signature_header(buyer.create_payment_payload(required))
    r = app.post("/read/v1/page", json={"url": "https://news.test/a"},
                 headers={"PAYMENT-SIGNATURE": header})  # fmt: skip
    assert r.status_code == 402 and decode_payment_required(r).error == reason
