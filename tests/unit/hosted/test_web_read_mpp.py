"""Web Read over MPP (Tempo): the same promise as x402, a page is paid for only when delivered.

A stub rail stands in for Tempo (the SDK path is exercised end to end against Tempo's testnet
separately); these tests pin the ordering: validate, deliver, and only then broadcast."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("x402")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _x402_fake_facilitator import FakeFacilitator  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
PAY_TO = "0x" + "7" * 40
PAYER = "0x" + "a" * 40
HTML = {"content-type": "text/html; charset=utf-8"}
PAGE = "<html><head><title>T</title></head><body><main><p>Hello agents.</p></main></body></html>"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


svc = _load("web_read_service", ROOT / "hosted" / "web_read" / "service.py")
fetch = sys.modules["web_read_fetch"]
rail_mod = sys.modules["web_read_mpp_rail"]


class StubRail:
    """Records what a real Tempo rail would do; `kind` is the credential type to report."""

    def __init__(self, kind: str = "transaction", valid: bool = True, settle_ok: bool = True):
        self.kind, self.valid, self.settle_ok = kind, valid, settle_ok
        self.validated, self.settled = 0, 0
        self.challenged: list[str] = []

    async def challenge_headers(self, url: str, method: str = "POST"):
        self.challenged.append(method)
        return [("WWW-Authenticate", 'Payment id="x", method="tempo", intent="charge"')]

    async def validate(self, authorization: str):
        self.validated += 1
        return (object(), PAYER, self.kind) if self.valid else None

    async def settle(self, credential):
        if not self.settle_ok:
            raise RuntimeError("broadcast failed")
        self.settled += 1
        return SimpleNamespace(reference="0x" + "b" * 64, payer=PAYER,
                               to_payment_receipt=lambda: "receipt-header")  # fmt: skip


def _client(tmp_path: Path, rail: StubRail, allow_get: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, text=PAGE, headers=HTML)

    async def dns(host: str, port: int):
        return {"localhost": ["127.0.0.1"]}.get(host, ["93.184.216.34"])

    settings = svc.Settings(pay_to=PAY_TO, resource_url="http://testserver/v1/page",
                            data_dir=tmp_path, operator_token="op",
                            allow_get=allow_get)  # fmt: skip
    reader = fetch.Fetcher(settings.user_agent, transport=httpx.MockTransport(handler), resolve=dns)
    app = svc.create_app(settings, facilitator=FakeFacilitator(), fetcher=reader, tempo_rail=rail)
    return TestClient(app)


PAY = {"Authorization": "Payment eyJjcmVkZW50aWFsIjoieCJ9"}


def _stats(c):
    return c.get("/v1/operator/stats", headers={"Authorization": "Bearer op"}).json()


def test_the_402_offers_mpp_next_to_x402(tmp_path):
    r = _client(tmp_path, StubRail()).post("/v1/page", json={"url": "https://site.test/a"})
    assert r.status_code == 402
    assert r.headers["payment-required"]  # x402
    assert r.headers["www-authenticate"].startswith("Payment ")  # MPP


def test_a_delivered_page_is_broadcast_once_and_recorded(tmp_path):
    rail = StubRail()
    c = _client(tmp_path, rail)
    r = c.post("/v1/page", json={"url": "https://site.test/a", "request_id": "j1"}, headers=PAY)
    assert r.status_code == 200, r.text
    assert r.headers["payment-receipt"] == "receipt-header"
    assert r.json()["receipt"]["rail"] == "mpp-tempo"
    assert r.json()["receipt"]["payment_ref"] == "tempo:0x" + "b" * 64
    assert rail.settled == 1
    s = _stats(c)
    assert s["settled_reads"] == 1 and s["distinct_payers"] == 1 and s["revenue_usd"] == "0.002"


def test_a_refused_page_is_never_broadcast(tmp_path):
    rail = StubRail()
    r = _client(tmp_path, rail).post("/v1/page", json={"url": "http://localhost/"}, headers=PAY)
    assert r.status_code == 422 and r.json()["charged"] is False
    assert rail.validated == 1 and rail.settled == 0


def test_an_invalid_credential_gets_a_fresh_challenge_and_no_fetch(tmp_path):
    rail = StubRail(valid=False)
    c = _client(tmp_path, rail)
    r = c.post("/v1/page", json={"url": "https://site.test/a"}, headers=PAY)
    assert r.status_code == 402 and r.json()["error"] == "payment_invalid"
    assert r.headers["www-authenticate"].startswith("Payment ")
    assert rail.settled == 0 and _stats(c)["funnel"]["payment_rejected"] == 1


def test_a_retry_from_the_same_payer_is_recovered_without_a_second_broadcast(tmp_path):
    rail = StubRail()
    c = _client(tmp_path, rail)
    body = {"url": "https://site.test/a", "request_id": "j1"}
    first = c.post("/v1/page", json=body, headers=PAY)
    again = c.post("/v1/page", json=body, headers=PAY)
    assert again.status_code == 200 and again.headers["x-inferrail-recovered"] == "1"
    assert again.json()["content_sha256"] == first.json()["content_sha256"]
    assert rail.settled == 1


def test_a_failed_broadcast_returns_no_content(tmp_path):
    rail = StubRail(settle_ok=False)
    c = _client(tmp_path, rail)
    r = c.post("/v1/page", json={"url": "https://site.test/a"}, headers=PAY)
    assert r.status_code == 402 and r.json() == {"error": "settlement_failed", "charged": False}
    assert _stats(c)["settled_reads"] == 0


def test_an_already_paid_hash_credential_that_cannot_be_delivered_is_owed_back(tmp_path):
    rail = StubRail(kind="hash")
    c = _client(tmp_path, rail)
    r = c.post("/v1/page", json={"url": "http://localhost/"}, headers=PAY)
    assert r.status_code == 422
    assert r.json()["charged"] is True and r.json()["refund"] == "owed"
    assert _stats(c)["funnel"]["refund_owed"] == 1


def test_x402_buyers_are_unaffected_when_mpp_is_on(tmp_path):
    rail = StubRail()
    c = _client(tmp_path, rail)
    r = c.post("/v1/page", json={"url": "https://site.test/a"},
               headers={"Authorization": "Bearer something-else"})  # fmt: skip
    assert r.status_code == 402 and r.headers["payment-required"]
    assert rail.validated == 0


@pytest.mark.parametrize(
    ("env", "error"),
    [
        ({"READ_MPP_CHAIN_ID": "1"}, "READ_MPP_CHAIN_ID"),
        ({"READ_MPP_CHAIN_ID": "4217"}, "READ_MPP_MAINNET_APPROVED"),
    ],
)
def test_mpp_settings_guard_the_chain_and_mainnet(env, error):
    with pytest.raises(RuntimeError, match=error):
        rail_mod.MppSettings.from_env(env, realm="x")


def test_mpp_is_off_by_default_and_its_secret_is_stable_across_restarts():
    assert rail_mod.MppSettings.from_env({}, realm="x") is None
    env = {"READ_MPP_CHAIN_ID": "42431", "CDP_API_KEY_SECRET": "s"}
    one, two = (rail_mod.MppSettings.from_env(env, realm="x") for _ in range(2))
    assert one.secret_key == two.secret_key and one.secret_key != "s"
    mainnet = rail_mod.MppSettings.from_env(
        {"READ_MPP_CHAIN_ID": "4217", "READ_MPP_MAINNET_APPROVED": "1"}, realm="x"
    )
    assert mainnet.chain_id == 4217


def test_replay_claims_survive_a_restart_and_temporary_records_expire(tmp_path, monkeypatch):
    import asyncio

    path = tmp_path / "mpp_replay.sqlite3"
    store = rail_mod.ReplayStore(path)
    assert asyncio.run(store.put_if_absent("mpp:charge:0xabc", "0xabc")) is True
    assert asyncio.run(store.put_if_absent("mpp:charge:0xabc", "0xabc")) is False
    restarted = rail_mod.ReplayStore(path)
    assert asyncio.run(restarted.get("mpp:charge:0xabc")) == "0xabc"
    assert asyncio.run(restarted.put_if_absent("mpp:charge:0xabc", "0xabc")) is False

    now = rail_mod.time.time()
    assert asyncio.run(restarted.put_if_absent_until("marker", {"tx": 1}, now + 60)) is True
    assert asyncio.run(restarted.put_if_absent_until("marker", {"tx": 2}, now + 60)) is False
    with pytest.raises(ValueError):
        asyncio.run(restarted.put_if_absent_until("late", 1, now - 1))
    monkeypatch.setattr(rail_mod.time, "time", lambda: now + 61)
    assert asyncio.run(restarted.get("marker")) is None
    assert asyncio.run(restarted.put_if_absent_until("marker", {"tx": 3}, now + 120)) is True
    assert asyncio.run(restarted.get("mpp:charge:0xabc")) == "0xabc"  # claims never expire
    asyncio.run(restarted.delete("marker"))
    assert asyncio.run(restarted.get("marker")) is None


def test_a_used_transaction_hash_stays_used_after_a_restart(tmp_path):
    """The real SDK, wired as in production: a hash credential's transfer, once claimed, can't
    buy a second page from a restarted process (the SDK's default store forgets it)."""
    import asyncio

    pytest.importorskip("mpp.methods.tempo")
    from mpp.errors import CredentialReplayedError
    from mpp.store import ExpiringStore

    def intents(rail):  # the SDK keeps each charge intent in its methods' closures
        cells = (c.cell_contents for m in rail.server.methods for c in m.broadcast.__closure__)
        return [v for v in cells if hasattr(v, "_reserve_hash")]

    cfg = rail_mod.MppSettings(chain_id=rail_mod.TESTNET, secret_key="k", realm="x")
    path = tmp_path / "mpp_replay.sqlite3"
    tx = "0x" + "c" * 64
    store = rail_mod.ReplayStore(path)
    assert isinstance(store, ExpiringStore)
    first = rail_mod.TempoRail(cfg, PAY_TO, "0.002", store)
    assert intents(first) and all(i._store is store for i in intents(first))
    asyncio.run(intents(first)[0]._reserve_hash(tx))

    restarted = rail_mod.TempoRail(cfg, PAY_TO, "0.002", rail_mod.ReplayStore(path))
    for intent in intents(restarted):
        with pytest.raises(CredentialReplayedError):
            asyncio.run(intent._ensure_hash_unused(tx))


def test_the_service_keeps_mpp_replay_keys_on_its_data_disk(tmp_path):
    pytest.importorskip("mpp.methods.tempo")
    settings = svc.Settings(
        pay_to=PAY_TO, resource_url="http://testserver/v1/page", data_dir=tmp_path,
        mpp=rail_mod.MppSettings(chain_id=rail_mod.TESTNET, secret_key="k", realm="x"),
    )  # fmt: skip
    svc.create_app(settings, facilitator=FakeFacilitator())
    assert (tmp_path / "mpp_replay.sqlite3").exists()


def test_mpp_over_get_offers_a_get_challenge_and_broadcasts_only_after_delivery(tmp_path):
    rail = StubRail()
    c = _client(tmp_path, rail, allow_get=True)
    probe = c.get("/v1/page")
    assert probe.status_code == 402 and probe.headers["www-authenticate"].startswith("Payment")
    assert rail.challenged == ["GET"]
    paid = c.get("/v1/page", params={"url": "https://news.test/a"},
                 headers={"Authorization": "Payment cred"})  # fmt: skip
    assert paid.status_code == 200, paid.text
    assert paid.json()["receipt"]["rail"] == "mpp-tempo" and rail.settled == 1
    refused = c.get("/v1/page", params={"url": "http://localhost/"},
                    headers={"Authorization": "Payment cred"})  # fmt: skip
    assert refused.status_code == 422 and rail.settled == 1  # never broadcast


def test_mpp_get_without_a_url_is_rejected_before_validation(tmp_path):
    rail = StubRail()
    c = _client(tmp_path, rail, allow_get=True)
    r = c.get("/v1/page", headers={"Authorization": "Payment cred"})
    assert r.status_code == 422 and rail.validated == 0 and rail.settled == 0


def test_the_mpp_challenge_describes_the_offer(tmp_path):
    # MPP indexes and clients list a seller by the challenge's description parameter.
    import asyncio
    import re

    pytest.importorskip("mpp.methods.tempo")
    cfg = rail_mod.MppSettings(chain_id=rail_mod.TESTNET, secret_key="k", realm="x")
    rail = rail_mod.TempoRail(cfg, PAY_TO, "0.002", description=svc.MPP_DESCRIPTION)
    headers = asyncio.run(rail.challenge_headers("http://x/v1/page", "GET"))
    assert len(headers) == 2  # one per currency
    for _, header in headers:
        assert dict(re.findall(r'(\w+)="([^"]*)"', header))["description"] == svc.MPP_DESCRIPTION
    plain = rail_mod.TempoRail(cfg, PAY_TO, "0.002")
    assert "description=" not in asyncio.run(plain.challenge_headers("http://x/v1/page"))[0][1]
