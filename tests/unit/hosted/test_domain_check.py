"""Domain Check: DNS records, SPF/DMARC and RDAP registration for one domain, sold as a Web Read
task. Proves the offer, delivery and receipt; that a nonexistent domain is a delivered answer;
that a resolver outage or an invalid or reserved name is never charged; that only registries
whose terms were reviewed are queried and others say not_covered; that registrant contacts are
never returned; that a partial failure is marked, never shown as empty; that a retry never pays
twice; that the domain is never recorded; and the mounting rules."""

from __future__ import annotations

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

domains = svc._sibling("domains")
cohost = _load("budget_gateway_cohost_domain", ROOT / "hosted" / "budget_gateway" / "cohost.py")
PAY_TO = "0x" + "6" * 40
SECRET = b"d" * 32

BOOTSTRAP = {"services": [
    [["com", "net"], ["https://rdap.verisign.com/com/v1/"]],
    [["org"], ["https://rdap.publicinterestregistry.org/rdap/"]],
    [["xyz"], ["https://rdap.centralnic.com/xyz/"]],
    [["dev", "app"], ["https://pubapi.registry.google/rdap/"]],
]}  # fmt: skip

RDAP_EXAMPLE = {
    "ldhName": "EXAMPLE.COM",
    "status": ["client delete prohibited", "client transfer prohibited"],
    "events": [{"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"},
               {"eventAction": "expiration", "eventDate": "2027-08-13T04:00:00Z"},
               {"eventAction": "last changed", "eventDate": "2026-01-16T18:26:50Z"}],
    "entities": [
        {"roles": ["registrar"], "publicIds": [{"type": "IANA Registrar ID", "identifier": "376"}],
         "vcardArray": ["vcard", [["version", {}, "text", "4.0"],
                                  ["fn", {}, "text", "Example Registrar"]]]},
        {"roles": ["registrant"], "vcardArray": ["vcard", [
            ["fn", {}, "text", "Jane Private"], ["email", {}, "text", "jane@private.test"]]]},
    ],
    "nameservers": [{"ldhName": "B.IANA-SERVERS.NET"}, {"ldhName": "A.IANA-SERVERS.NET"}],
    "secureDNS": {"delegationSigned": True},
    "notices": [{"title": "Terms of Service", "description": ["Service subject to Terms of Use."],
                 "links": [{"href": "https://www.verisign.com/rdap-terms"}]}],
}  # fmt: skip

ZONE = {
    ("example.com", "A"): [{"type": 1, "TTL": 300, "data": "93.184.215.14"}],
    ("example.com", "AAAA"): [{"type": 28, "TTL": 300, "data": "2606:2800:21f:cb07::1"}],
    ("example.com", "MX"): [{"type": 15, "TTL": 300, "data": "0 ."}],
    ("example.com", "NS"): [{"type": 2, "TTL": 300, "data": "a.iana-servers.net."}],
    ("example.com", "TXT"): [{"type": 16, "TTL": 300, "data": '"v=spf1 -all"'},
                             {"type": 16, "TTL": 300, "data": '"site-verify" "=abc"'}],
    ("_dmarc.example.com", "TXT"): [{"type": 16, "TTL": 300,
                                     "data": '"v=DMARC1;p=reject;sp=reject"'}],
}  # fmt: skip


def _net(zone=None, nx=(), dns_fail=False, fail_types=(), rdap=None):
    """A fake internet: Cloudflare DoH answers from `zone` (names in `nx` are NXDOMAIN), the IANA
    bootstrap, and RDAP registries answering from `rdap` {host: {apex: body | 404}}."""
    zone = ZONE if zone is None else zone
    rdap = {"rdap.verisign.com": {"example.com": RDAP_EXAMPLE}} if rdap is None else rdap
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "cloudflare-dns.com":
            name, rtype = request.url.params["name"], request.url.params["type"]
            seen.append(f"dns {name} {rtype}")
            if dns_fail or rtype in fail_types:
                return httpx.Response(503)
            if any(name == n or name.endswith("." + n) for n in nx):
                return httpx.Response(200, json={"Status": 3, "AD": False})
            return httpx.Response(200, json={"Status": 0, "AD": True,
                                             "Answer": zone.get((name, rtype), [])})  # fmt: skip
        if host == "data.iana.org":
            seen.append("bootstrap")
            return httpx.Response(200, json=BOOTSTRAP)
        seen.append(f"rdap {host}{path}")
        body = (rdap.get(host) or {}).get(path.rsplit("/", 1)[-1], 404)
        return httpx.Response(404) if body == 404 else httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def _client(tmp_path: Path, net: httpx.AsyncClient, fac: FakeFacilitator):
    settings = svc.Settings(pay_to=PAY_TO, resource_url="http://testserver/v1/lookup",
                            data_dir=tmp_path, operator_token="op")  # fmt: skip
    task = domains.domain_task(svc, SECRET, client=net)
    return TestClient(svc.create_app(settings, facilitator=fac, task=task))


def _buy(c: TestClient, body: dict, account=None):
    unpaid = c.post("/v1/lookup", json=body)
    assert unpaid.status_code == 402, unpaid.text
    signed = sign_payment(decode_payment_required(unpaid), account)
    return c.post("/v1/lookup", json=body, headers={"PAYMENT-SIGNATURE": signed.header})


ONE = {"domain": "example.com"}


def test_unpaid_request_gets_the_offer(tmp_path):
    net, seen = _net()
    r = _client(tmp_path, net, FakeFacilitator()).post("/v1/lookup", json=ONE)
    assert r.status_code == 402 and decode_payment_required(r).accepts[0].amount == "2000"
    assert seen == []  # nothing is looked up before payment


def test_a_paid_lookup_returns_dns_email_and_registration_and_settles_once(tmp_path):
    net, _ = _net()
    fac = FakeFacilitator()
    r = _buy(_client(tmp_path, net, fac), ONE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["domain"] == "example.com" and body["resolves"] is True and body["complete"]
    assert body["dnssec_validated"] is True
    assert body["dns"]["A"] == {"status": "ok", "values": ["93.184.215.14"], "ttl": 300}
    assert body["dns"]["CNAME"]["status"] == "no_records"
    assert body["dns"]["TXT"]["values"] == ["v=spf1 -all", "site-verify=abc"]  # strings joined
    assert body["email"] == {"spf": "v=spf1 -all", "dmarc": "v=DMARC1;p=reject;sp=reject",
                             "dmarc_policy": "reject", "mx_count": 1}  # fmt: skip
    reg = body["registration"]
    assert body["registration_status"] == "ok" and reg["registry"] == "Verisign"
    assert reg["registrar"] == "Example Registrar" and reg["registrar_iana_id"] == "376"
    assert reg["created"] == "1995-08-14T04:00:00Z" and reg["expires"].startswith("2027")
    assert reg["age_days"] > 11000 and reg["dnssec"] is True
    assert reg["nameservers"] == ["a.iana-servers.net", "b.iana-servers.net"]
    assert reg["terms_url"] == "https://www.verisign.com/rdap-terms"
    assert body["receipt"]["price_usd"] == "0.002" and len(fac.calls_to("settle")) == 1


def test_registrant_contacts_are_never_returned(tmp_path):
    net, _ = _net()
    text = _buy(_client(tmp_path, net, FakeFacilitator()), ONE).text
    assert "Jane Private" not in text and "jane@private.test" not in text


def test_a_nonexistent_domain_is_a_delivered_answer(tmp_path):
    net, _ = _net(nx=("nope-zz9.com",), rdap={})
    fac = FakeFacilitator()
    r = _buy(_client(tmp_path, net, fac), {"domain": "nope-zz9.com"})
    body = r.json()
    assert r.status_code == 200 and body["resolves"] is False
    assert body["dns"]["A"]["status"] == "nxdomain"
    assert body["registration_status"] == "not_registered" and body["registration"] is None
    assert len(fac.calls_to("settle")) == 1


@pytest.mark.parametrize(("domain", "host"), [("abc.xyz", "rdap.centralnic.com"),
                                              ("web.dev", "pubapi.registry.google")])  # fmt: skip
def test_registries_whose_terms_were_not_cleared_are_never_queried(tmp_path, domain, host):
    net, seen = _net(zone={})
    body = _buy(_client(tmp_path, net, FakeFacilitator()), {"domain": domain}).json()
    assert body["registration_status"] == "not_covered" and body["registration"] is None
    assert not any(host in s for s in seen)
    assert "not that the domain is unregistered" in body["note"]


def test_a_subdomain_registration_is_read_from_its_registered_name(tmp_path):
    net, seen = _net()
    body = _buy(_client(tmp_path, net, FakeFacilitator()), {"domain": "WWW.Example.COM."}).json()
    assert body["domain"] == "www.example.com" and body["registration_status"] == "ok"
    assert "rdap rdap.verisign.com/com/v1/domain/example.com" in seen


def test_a_partial_dns_failure_is_marked_never_shown_empty(tmp_path):
    net, _ = _net(fail_types=("MX",))
    body = _buy(_client(tmp_path, net, FakeFacilitator()), ONE).json()
    assert body["dns"]["MX"]["status"] == "error" and body["complete"] is False
    assert body["dns"]["A"]["status"] == "ok"


def test_a_registry_failure_is_error_not_unregistered(tmp_path):
    net, _ = _net(rdap={"rdap.verisign.com": {}})  # 404 would be not_registered...
    body = _buy(_client(tmp_path, net, FakeFacilitator()), {"domain": "example.com"}).json()
    assert body["registration_status"] == "not_registered"

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.host == "rdap.verisign.com":
            return httpx.Response(503)  # ...but a registry outage is unknown, never "free"
        return httpx.Response(200, json=BOOTSTRAP if request.url.host == "data.iana.org"
                              else {"Status": 0, "Answer": []})  # fmt: skip

    c = _client(tmp_path / "b", httpx.AsyncClient(transport=httpx.MockTransport(broken)),
                FakeFacilitator())  # fmt: skip
    body = _buy(c, ONE).json()
    assert body["registration_status"] == "error" and body["complete"] is False


def test_registration_can_be_skipped_and_types_narrowed(tmp_path):
    net, seen = _net()
    req = {"domain": "example.com", "records": ["MX"], "registration": False}
    body = _buy(_client(tmp_path, net, FakeFacilitator()), req).json()
    assert list(body["dns"]) == ["MX"] and body["registration_status"] == "skipped"
    assert body["email"]["spf"] == "v=spf1 -all"  # SPF still looked up
    assert not any(s.startswith("rdap") or s == "bootstrap" for s in seen)


def test_a_resolver_outage_is_never_charged(tmp_path):
    net, _ = _net(dns_fail=True)
    fac = FakeFacilitator()
    r = _buy(_client(tmp_path, net, fac), ONE)
    assert r.status_code == 502 and r.json()["error"] == "upstream_failed"
    assert r.json()["charged"] is False and fac.calls_to("settle") == []


@pytest.mark.parametrize(
    "domain",
    ["https://example.com/x", "example.com:443", "user@example.com", "localhost",
     "printer.local", "db.internal", "nodot", "-bad-.com", "a" * 64 + ".com", "10.0.0.1",
     "x.onion", "example.123"],
)  # fmt: skip
def test_an_invalid_or_reserved_name_is_never_charged(tmp_path, domain):
    net, seen = _net()
    fac = FakeFacilitator()
    c = _client(tmp_path, net, fac)
    signed = sign_payment(decode_payment_required(c.post("/v1/lookup", json=ONE)))
    r = c.post("/v1/lookup", json={"domain": domain},
               headers={"PAYMENT-SIGNATURE": signed.header})  # fmt: skip
    assert r.status_code == 422 and fac.calls_to("settle") == [] and seen == []


def test_an_internationalized_name_is_looked_up_in_its_ascii_form(tmp_path):
    net, seen = _net(zone={})
    body = _buy(_client(tmp_path, net, FakeFacilitator()), {"domain": "bücher.com"}).json()
    assert body["domain"] == "xn--bcher-kva.com"
    assert "dns xn--bcher-kva.com A" in seen


def test_a_retry_returns_the_paid_lookup_without_a_second_charge(tmp_path):
    net, seen = _net()
    fac = FakeFacilitator()
    c = _client(tmp_path, net, fac)
    buyer = Account.create()
    body = {**ONE, "request_id": "job-7"}
    first, again = _buy(c, body, buyer), _buy(c, body, buyer)  # the response was lost
    assert first.status_code == again.status_code == 200
    assert first.json()["results_sha256"] == again.json()["results_sha256"]
    assert len(fac.calls_to("settle")) == 1 and seen.count("dns example.com A") == 1


def test_repeat_lookups_are_cached_so_registries_see_one_query(tmp_path):
    net, seen = _net()
    c = _client(tmp_path, net, FakeFacilitator())
    _buy(c, {**ONE, "request_id": "a"})
    _buy(c, {**ONE, "request_id": "b"})  # a new, separately paid lookup
    assert sum(s.startswith("rdap ") for s in seen) == 1 and seen.count("bootstrap") == 1


def test_the_sales_ledger_holds_no_domain(tmp_path):
    net, _ = _net(zone={}, rdap={})
    _buy(_client(tmp_path, net, FakeFacilitator()), {"domain": "secret-launch-name.com"})
    db = sqlite3.connect(tmp_path / "web_read.sqlite3")
    rows = db.execute("SELECT url, final_url, words, sha256 FROM reads").fetchall()
    assert rows and all(r[0] is None and r[1] is None for r in rows)
    assert "secret-launch-name" not in "\n".join(str(r) for r in rows)


def test_the_published_request_schema_is_self_contained():
    import json as _json

    schema = _json.dumps(domains.LookupRequest.model_json_schema())
    assert "$ref" not in schema and "$defs" not in schema


def test_a_short_secret_is_refused():
    with pytest.raises(ValueError):
        domains.domain_task(svc, b"short")


def test_only_reviewed_registries_are_allowed():
    # Adding a registry is a deliberate act: read its RDAP terms first (see the module docstring).
    assert set(domains.RDAP_HOSTS) == {"rdap.verisign.com", "rdap.publicinterestregistry.org",
                                       "rdap.identitydigital.services"}  # fmt: skip


# -- mounted on the shared origin ---------------------------------------------------------------
HOSTED_PAY_TO = "0x" + "9" * 40
WEB_READ_PAY_TO = "0xA45140E99E9482a50BC3fA5F6000c02FAAB6F65E"
FINDER_BASE = "0x54BF9C490112CD6a54Cb8f22D6bf83621ABe77CF"
PKG_BASE = "0xe9c1249648257C1C1D70F8b7682851C377bb8f53"
ENV = {"DOMAIN_PAY_TO": PAY_TO, "DOMAIN_MPP_CHAIN_ID": "42431", "BG_OPERATOR_TOKEN": "op"}


def _hosted(tmp_path: Path):
    trial_mod, hosted_mod = cohost.load_modules()
    settings = hosted_mod.Settings.from_env(
        {"BG_DATA_DIR": str(tmp_path / "hosted"), "BG_MOUNT_PREFIX": "/hosted",
         "BG_PUBLIC_BASE_URL": "http://testserver/hosted", "BG_X402_PAY_TO": HOSTED_PAY_TO}
    )  # fmt: skip
    return trial_mod, hosted_mod, settings


def test_not_mounted_without_its_payto(tmp_path):
    assert cohost.domain_check_from_env(_hosted(tmp_path)[2], {}) is None


@pytest.mark.parametrize(
    "change",
    [
        {"DOMAIN_MPP_CHAIN_ID": None},  # MPP only by default: needs MPP
        {"DOMAIN_MPP_CHAIN_ID": "4217"},  # Tempo mainnet needs its own approval
        {"DOMAIN_X402": "1", "DOMAIN_X402_PAY_TO": HOSTED_PAY_TO},
        {"DOMAIN_X402": "1", "DOMAIN_X402_PAY_TO": WEB_READ_PAY_TO, "READ_PAY_TO": WEB_READ_PAY_TO},
        {"DOMAIN_X402": "1", "DOMAIN_X402_PAY_TO": FINDER_BASE, "FINDER_X402_PAY_TO": FINDER_BASE},
        {"DOMAIN_X402": "1", "DOMAIN_X402_PAY_TO": PKG_BASE, "PKG_X402_PAY_TO": PKG_BASE},
        {"DOMAIN_X402_PAY_TO": "0x" + "5" * 40},  # an x402 address while x402 is off
        {"BG_OPERATOR_TOKEN": None},  # no stable retry secret
    ],
)
def test_misconfiguration_leaves_it_unmounted(tmp_path, change):
    env = {**ENV, "DOMAIN_DATA_DIR": str(tmp_path / "d")}
    for k, v in change.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    assert cohost.domain_check_from_env(_hosted(tmp_path)[2], env) is None


def test_package_check_refuses_domain_checks_base_address(tmp_path):
    own = "0x" + "4" * 40
    env = {"PKG_PAY_TO": PAY_TO, "PKG_MPP_CHAIN_ID": "42431", "BG_OPERATOR_TOKEN": "op",
           "PKG_X402": "1", "PKG_X402_PAY_TO": own, "DOMAIN_X402_PAY_TO": own,
           "PKG_DATA_DIR": str(tmp_path / "p")}  # fmt: skip
    assert cohost.package_check_from_env(_hosted(tmp_path)[2], env) is None


def test_mounted_mpp_only_and_listed_on_the_origin(tmp_path):
    trial_mod, hosted_mod, settings = _hosted(tmp_path)
    env = {**ENV, "DOMAIN_DATA_DIR": str(tmp_path / "d")}
    mounted = cohost.domain_check_from_env(settings, env)
    assert mounted is not None and mounted[1].x402 is False and mounted[1].price_usd == "0.002"
    app = cohost.create_combined_app(
        tmp_path / "trial", settings, facilitator=FakeFacilitator(),
        modules=(trial_mod, hosted_mod), web_read=None, web_search=None, finder=None,
        packages=None, domains=mounted, godmode=None,
    )  # fmt: skip
    c = TestClient(app)
    unpaid = c.post("/domain/v1/lookup", json=ONE)
    assert unpaid.status_code == 402 and unpaid.headers["www-authenticate"].startswith("Payment ")
    doc = c.get("/openapi.json").json()
    op = doc["paths"]["/domain/v1/lookup"]["post"]
    assert op["operationId"] == "lookupDomain"
    assert op["x-payment-info"]["price"]["amount"] == "0.002"
    assert "Domain Check" in doc["info"]["description"]
    assert "/domain/v1/lookup" in doc["info"]["x-guidance"]
    assert "Inferrail Domain Check" in c.get("/llms.txt").text
    assert "not_covered" in c.get("/domain/llms.txt").text


def test_base_x402_pays_its_own_address(tmp_path):
    recipients = []
    fac = FakeFacilitator(on_settle=lambda p: recipients.append(authorization_of(p)["to"]))
    own = "0x" + "5" * 40
    settings = svc.Settings(
        pay_to=WEB_READ_PAY_TO, x402_pay_to=own,
        resource_url="http://testserver/v1/lookup", data_dir=tmp_path,
    )  # fmt: skip
    net, _ = _net()
    c = TestClient(svc.create_app(settings, facilitator=fac,
                                  task=domains.domain_task(svc, SECRET, client=net)))  # fmt: skip
    (accept,) = decode_payment_required(c.post("/v1/lookup", json=ONE)).accepts
    assert accept.pay_to == own
    assert _buy(c, ONE).status_code == 200 and [r.lower() for r in recipients] == [own]


def test_the_skill_states_the_live_contract():
    skill = (ROOT / "skills" / "domain-check" / "SKILL.md").read_text()
    head = skill.split("---")[1]
    assert "name: inferrail-domain-check" in head and "description:" in head
    assert "$0.002" in skill and "/domain/v1/lookup" in skill
    assert "not_covered" in skill and "Registrant contacts" in skill
