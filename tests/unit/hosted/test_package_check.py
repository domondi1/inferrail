"""Package Check: known vulnerabilities for package versions, from OSV.dev, sold as a Web Read task.
Proves the offer, delivery and receipt, that a clean result is a delivered (charged) answer, that an
OSV outage or an invalid request is never charged, that restricted-licence records are dropped,
that a retry never pays twice, that the package list is never recorded, and the mounting rules."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

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
from test_web_read import svc  # noqa: E402
from test_web_search import ROOT, _load  # noqa: E402

packages = svc._sibling("packages")
cohost = _load("budget_gateway_cohost_pkg", ROOT / "hosted" / "budget_gateway" / "cohost.py")
PAY_TO = "0x" + "6" * 40
SECRET = b"p" * 32

LODASH = {
    "id": "GHSA-35jh-r3h4-6jhm",
    "summary": "Command Injection in lodash",
    "aliases": ["CVE-2021-23337"],
    "database_specific": {"severity": "HIGH"},
    "affected": [{"package": {"ecosystem": "npm", "name": "lodash"},
                  "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"},
                                                           {"fixed": "4.17.21"}]}]}],
}  # fmt: skip


def _osv(batch: dict | None = None, fail: bool = False, details: dict | None = None,
         pages: dict | None = None):  # fmt: skip
    """A fake OSV.dev: querybatch returns `batch` (or a 503), vulns/{id} returns `details`,
    query with a page_token returns pages[token] (a missing token answers 503)."""
    seen: list[str] = []
    details = {"GHSA-35jh-r3h4-6jhm": LODASH} if details is None else details

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/querybatch":
            if fail:
                return httpx.Response(503)
            return httpx.Response(200, json=batch)
        if request.url.path == "/v1/query":
            token = json.loads(request.content).get("page_token")
            page = (pages or {}).get(token)
            return httpx.Response(200, json=page) if page is not None else httpx.Response(503)
        vid = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=details[vid]) if vid in details else httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def _client(tmp_path: Path, osv: httpx.AsyncClient, fac: FakeFacilitator):
    settings = svc.Settings(pay_to=PAY_TO, resource_url="http://testserver/v1/check",
                            data_dir=tmp_path, operator_token="op")  # fmt: skip
    task = packages.package_task(svc, SECRET, client=osv)
    return TestClient(svc.create_app(settings, facilitator=fac, task=task))


def _buy(c: TestClient, body: dict, account=None):
    unpaid = c.post("/v1/check", json=body)
    assert unpaid.status_code == 402, unpaid.text
    signed = sign_payment(decode_payment_required(unpaid), account)
    return c.post("/v1/check", json=body, headers={"PAYMENT-SIGNATURE": signed.header})


ONE = {"packages": [{"ecosystem": "npm", "name": "lodash", "version": "4.17.15"}]}
TWO = {"packages": [{"ecosystem": "npm", "name": "lodash", "version": "4.17.15"},
                    {"ecosystem": "PyPI", "name": "requests", "version": "2.32.3"}]}  # fmt: skip
TWO_BATCH = {"results": [{"vulns": [{"id": "GHSA-35jh-r3h4-6jhm"}]}, {}]}


def test_unpaid_request_gets_the_offer(tmp_path):
    osv, seen = _osv()
    r = _client(tmp_path, osv, FakeFacilitator()).post("/v1/check", json=ONE)
    assert r.status_code == 402 and decode_payment_required(r).accepts[0].amount == "2000"
    assert seen == []  # nothing is looked up before payment


def test_a_paid_check_reports_vulnerabilities_and_settles_once(tmp_path):
    osv, _ = _osv(TWO_BATCH)
    fac = FakeFacilitator()
    r = _buy(_client(tmp_path, osv, fac), TWO)
    assert r.status_code == 200, r.text
    body = r.json()
    lodash, requests = body["results"]
    assert lodash["vulnerable"] is True and lodash["vuln_count"] == 1
    (v,) = lodash["vulns"]
    assert v["id"] == "GHSA-35jh-r3h4-6jhm" and v["aliases"] == ["CVE-2021-23337"]
    assert v["severity"] == "HIGH" and v["fixed"] == ["4.17.21"]
    assert v["url"] == "https://osv.dev/vulnerability/GHSA-35jh-r3h4-6jhm"
    assert (v["database"], v["license"]) == ("GitHub Advisory Database", "CC-BY-4.0")
    assert (lodash["status"], lodash["complete"]) == ("vulnerable", True)
    assert requests["vulnerable"] is False and requests["vulns"] == []
    assert (requests["status"], requests["complete"]) == ("no_known_vulnerabilities", True)
    assert "not a guarantee" in body["note"]
    assert body["checked"] == 2 and body["vulnerable_packages"] == 1 and "OSV.dev" in body["source"]
    assert body["receipt"]["price_usd"] == "0.002" and len(fac.calls_to("settle")) == 1


def test_a_clean_result_is_a_delivered_answer(tmp_path):
    osv, _ = _osv({"results": [{}]})
    fac = FakeFacilitator()
    r = _buy(_client(tmp_path, osv, fac), ONE)
    assert r.status_code == 200 and r.json()["results"][0]["vulnerable"] is False
    assert len(fac.calls_to("settle")) == 1


def test_paged_results_are_fetched_to_completion(tmp_path):
    batch = {"results": [{"vulns": [{"id": "GHSA-35jh-r3h4-6jhm"}], "next_page_token": "t1"}]}
    pages = {"t1": {"vulns": [{"id": "GHSA-more"}], "next_page_token": "t2"},
             "t2": {"vulns": [{"id": "GHSA-last"}]}}  # fmt: skip
    osv, _ = _osv(batch, pages=pages)
    res = _buy(_client(tmp_path, osv, FakeFacilitator()), ONE).json()["results"][0]
    assert [v["id"] for v in res["vulns"]] == ["GHSA-35jh-r3h4-6jhm", "GHSA-more", "GHSA-last"]
    assert res["complete"] is True and res["status"] == "vulnerable"


def test_a_page_that_cannot_be_fetched_is_marked_incomplete_never_clean(tmp_path):
    batch = {"results": [{"vulns": [{"id": "GHSA-35jh-r3h4-6jhm"}], "next_page_token": "lost"}]}
    osv, _ = _osv(batch, pages={})
    res = _buy(_client(tmp_path, osv, FakeFacilitator()), ONE).json()["results"][0]
    assert res["complete"] is False and res["vulnerable"] is True and res["vuln_count"] == 1


def test_an_empty_but_paged_result_is_incomplete_not_clean(tmp_path):
    # Defensive: a row that has no advisories yet still says there are more.
    osv, _ = _osv({"results": [{"next_page_token": "lost"}]}, pages={})
    res = _buy(_client(tmp_path, osv, FakeFacilitator()), ONE).json()["results"][0]
    assert res["status"] == "incomplete" and res["complete"] is False


def test_an_osv_outage_is_never_charged(tmp_path):
    osv, _ = _osv(fail=True)
    fac = FakeFacilitator()
    r = _buy(_client(tmp_path, osv, fac), ONE)
    assert r.status_code == 502 and r.json()["error"] == "upstream_failed"
    assert r.json()["charged"] is False and fac.calls_to("settle") == []


def test_restricted_licence_records_are_dropped(tmp_path):
    batch = {"results": [{"vulns": [{"id": "CGA-abc"}, {"id": "UBUNTU-CVE-1"},
                                    {"id": "GHSA-35jh-r3h4-6jhm"}]}]}  # fmt: skip
    osv, seen = _osv(batch)
    r = _buy(_client(tmp_path, osv, FakeFacilitator()), ONE)
    assert [v["id"] for v in r.json()["results"][0]["vulns"]] == ["GHSA-35jh-r3h4-6jhm"]
    assert not any("CGA-" in s or "UBUNTU-" in s for s in seen)  # never even fetched


def test_a_missing_detail_degrades_to_id_and_link(tmp_path):
    osv, _ = _osv({"results": [{"vulns": [{"id": "GHSA-gone"}]}]}, details={})
    r = _buy(_client(tmp_path, osv, FakeFacilitator()), ONE)
    (v,) = r.json()["results"][0]["vulns"]
    assert v["id"] == "GHSA-gone" and v["summary"] is None and v["url"].endswith("GHSA-gone")


@pytest.mark.parametrize(
    "body",
    [{"packages": [{"ecosystem": "Debian", "name": "openssl", "version": "1"}]},  # OS feed
     {"packages": []},
     {"packages": [{"ecosystem": "npm", "name": "x", "version": "1"}] * 21}],  # over the batch cap
)  # fmt: skip
def test_an_invalid_request_is_never_charged(tmp_path, body):
    osv, seen = _osv({"results": []})
    fac = FakeFacilitator()
    c = _client(tmp_path, osv, fac)
    unpaid = c.post("/v1/check", json=ONE)
    signed = sign_payment(decode_payment_required(unpaid))
    r = c.post("/v1/check", json=body, headers={"PAYMENT-SIGNATURE": signed.header})
    assert r.status_code == 422 and fac.calls_to("settle") == [] and seen == []


def test_a_retry_returns_the_paid_check_without_a_second_charge(tmp_path):
    osv, seen = _osv(TWO_BATCH)
    fac = FakeFacilitator()
    c = _client(tmp_path, osv, fac)
    buyer = Account.create()
    body = {**TWO, "request_id": "build-42"}
    first, again = _buy(c, body, buyer), _buy(c, body, buyer)  # the response was lost
    assert first.status_code == again.status_code == 200
    assert first.json()["results_sha256"] == again.json()["results_sha256"]
    assert len(fac.calls_to("settle")) == 1 and seen.count("POST /v1/querybatch") == 1


def test_the_sales_ledger_holds_no_package_names(tmp_path):
    # The paid result (which names the packages) is kept only for the recovery window; the
    # permanent sales record keeps counts and a hash.
    osv, _ = _osv({"results": [{}]})
    body = {"packages": [{"ecosystem": "npm", "name": "secret-internal-pkg", "version": "9.9.9"}]}
    _buy(_client(tmp_path, osv, FakeFacilitator()), body)
    db = sqlite3.connect(tmp_path / "web_read.sqlite3")
    rows = db.execute("SELECT url, final_url, words, sha256 FROM reads").fetchall()
    assert rows and all(r[0] is None and r[1] is None for r in rows)
    assert "secret-internal-pkg" not in "\n".join(str(r) for r in rows)


def test_the_published_request_schema_is_self_contained():
    import json as _json

    schema = _json.dumps(packages.CheckRequest.model_json_schema())
    assert "$ref" not in schema and "$defs" not in schema


def test_a_short_secret_is_refused():
    with pytest.raises(ValueError):
        packages.package_task(svc, b"short")


# -- mounted on the shared origin ---------------------------------------------------------------
HOSTED_PAY_TO = "0x" + "9" * 40
WEB_READ_PAY_TO = "0xA45140E99E9482a50BC3fA5F6000c02FAAB6F65E"
FINDER_BASE = "0x54BF9C490112CD6a54Cb8f22D6bf83621ABe77CF"
ENV = {"PKG_PAY_TO": PAY_TO, "PKG_MPP_CHAIN_ID": "42431", "BG_OPERATOR_TOKEN": "op"}


def _hosted(tmp_path: Path):
    trial_mod, hosted_mod = cohost.load_modules()
    settings = hosted_mod.Settings.from_env(
        {"BG_DATA_DIR": str(tmp_path / "hosted"), "BG_MOUNT_PREFIX": "/hosted",
         "BG_PUBLIC_BASE_URL": "http://testserver/hosted", "BG_X402_PAY_TO": HOSTED_PAY_TO}
    )  # fmt: skip
    return trial_mod, hosted_mod, settings


def test_not_mounted_without_its_payto(tmp_path):
    assert cohost.package_check_from_env(_hosted(tmp_path)[2], {}) is None


@pytest.mark.parametrize(
    "change",
    [
        {"PKG_MPP_CHAIN_ID": None},  # MPP only by default: needs MPP
        {"PKG_MPP_CHAIN_ID": "4217"},  # Tempo mainnet needs its own approval
        {"PKG_X402": "1", "PKG_X402_PAY_TO": HOSTED_PAY_TO},  # Hosted's address
        {"PKG_X402": "1", "PKG_X402_PAY_TO": WEB_READ_PAY_TO, "READ_PAY_TO": WEB_READ_PAY_TO},
        {"PKG_X402": "1", "PKG_X402_PAY_TO": FINDER_BASE, "FINDER_X402_PAY_TO": FINDER_BASE},
        {"PKG_X402_PAY_TO": "0x" + "5" * 40},  # an x402 address while x402 is off
        {"BG_OPERATOR_TOKEN": None},  # no stable retry secret
    ],
)
def test_misconfiguration_leaves_it_unmounted(tmp_path, change):
    env = {**ENV, "PKG_DATA_DIR": str(tmp_path / "p")}
    for k, v in change.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    assert cohost.package_check_from_env(_hosted(tmp_path)[2], env) is None


def test_mounted_mpp_only_and_listed_on_the_origin(tmp_path):
    trial_mod, hosted_mod, settings = _hosted(tmp_path)
    mounted = cohost.package_check_from_env(settings, {**ENV, "PKG_DATA_DIR": str(tmp_path / "p")})
    assert mounted is not None and mounted[1].x402 is False and mounted[1].price_usd == "0.002"
    app = cohost.create_combined_app(
        tmp_path / "trial", settings, facilitator=FakeFacilitator(),
        modules=(trial_mod, hosted_mod), web_read=None, web_search=None, finder=None,
        packages=mounted, godmode=None,
    )  # fmt: skip
    c = TestClient(app)
    unpaid = c.post("/pkg/v1/check", json=ONE)
    assert unpaid.status_code == 402 and unpaid.headers["www-authenticate"].startswith("Payment ")
    doc = c.get("/openapi.json").json()
    op = doc["paths"]["/pkg/v1/check"]["post"]
    assert op["operationId"] == "checkPackages"
    assert op["x-payment-info"]["price"]["amount"] == "0.002"
    assert "Package Check" in doc["info"]["description"]
    assert "/pkg/v1/check" in doc["info"]["x-guidance"]
    assert "Inferrail Package Check" in c.get("/llms.txt").text
    assert "OSV.dev" in c.get("/pkg/llms.txt").text


def test_base_x402_pays_its_own_address(tmp_path):
    recipients = []
    fac = FakeFacilitator(on_settle=lambda p: recipients.append(authorization_of(p)["to"]))
    own = "0x" + "5" * 40
    settings = svc.Settings(
        pay_to=WEB_READ_PAY_TO, x402_pay_to=own,
        resource_url="http://testserver/v1/check", data_dir=tmp_path,
    )  # fmt: skip
    osv, _ = _osv({"results": [{}]})
    c = TestClient(svc.create_app(settings, facilitator=fac,
                                  task=packages.package_task(svc, SECRET, client=osv)))  # fmt: skip
    (accept,) = decode_payment_required(c.post("/v1/check", json=ONE)).accepts
    assert accept.pay_to == own
    assert _buy(c, ONE).status_code == 200 and [r.lower() for r in recipients] == [own]
