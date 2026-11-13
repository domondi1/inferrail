"""God Mode: the commerce event recorder on the paid path, and the private operator view.

Synthetic data only: a fake x402 facilitator, a stub Tempo rail, mocked sites and a mocked RPC.
No real payment is made and no network is used."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

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

ROOT = Path(__file__).resolve().parents[3]
PAY_TO = "0x" + "7" * 40
PAYER = "0x" + "a" * 40
SECRET = "s" * 40
ARCHIVE = "t" * 40
PRIVATE_URL = "https://news.test/secret-path-zq9"
ARTICLE = "<html><head><title>T</title></head><body><main><p>Agents pay.</p></main></body></html>"
HTML = {"content-type": "text/html; charset=utf-8"}


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


svc = _load("web_read_service", ROOT / "hosted" / "web_read" / "service.py")
ce = sys.modules["web_read_commerce_events"]
gmod = _load("web_read_godmode", ROOT / "hosted" / "web_read" / "godmode.py")
fetch = sys.modules["web_read_fetch"]


def _site() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/missing":
            return httpx.Response(404)
        return httpx.Response(200, text=ARTICLE, headers=HTML)

    return httpx.MockTransport(handler)


async def _dns(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def _settings(tmp_path: Path, **kw):
    base = dict(pay_to=PAY_TO, resource_url="http://testserver/v1/page", data_dir=tmp_path,
                operator_token="op")  # fmt: skip
    return svc.Settings(**{**base, **kw})


def _x402_app(tmp_path: Path, fac=None, recorder="store", **kw):
    settings = _settings(tmp_path, **kw)
    reader = fetch.Fetcher(settings.user_agent, transport=_site(), resolve=_dns)
    store = ce.EventStore(tmp_path / "commerce_events.sqlite3") if recorder == "store" else recorder
    app = svc.create_app(settings, facilitator=fac or FakeFacilitator(), fetcher=reader,
                         recorder=store)  # fmt: skip
    return TestClient(app), store, settings


def _sign(c: TestClient, url: str):
    unpaid = c.post("/v1/page", json={"url": url})
    assert unpaid.status_code == 402
    return sign_payment(decode_payment_required(unpaid)).header


def _types(store, pid=None):
    q = "SELECT type FROM events" + (" WHERE purchase_id=?" if pid else "") + " ORDER BY seq"
    return [r["type"] for r in store.rows(q, (pid,) if pid else ())]


def _evidence(store, settings, **gm):
    g = gmod.GodModeSettings(login_secret=SECRET, **gm)
    return gmod.Evidence(store, settings.data_dir / "web_read.sqlite3", settings, g)


# -- the event store ------------------------------------------------------------------------------


def test_events_are_append_only_and_hash_chained(tmp_path):
    store = ce.EventStore(tmp_path / "e.sqlite3")
    store.append("payment_attempt", purchase_id="p1", rail="x402")
    store.append("delivered", purchase_id="p1", rail="x402")
    assert store.verify_chain() == {"ok": True, "checked": 2, "first_bad_seq": None}
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._db.execute("UPDATE events SET rail='mpp' WHERE seq=1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._db.execute("DELETE FROM events WHERE seq=1")
    # Someone with file access drops the guard and edits history: verification shows where.
    store._db.execute("DROP TRIGGER events_no_update")
    store._db.execute("UPDATE events SET rail='mpp' WHERE seq=1")
    assert store.verify_chain() == {"ok": False, "checked": 1, "first_bad_seq": 1}


def test_a_purchase_settles_and_delivers_once_and_retries_are_numbered(tmp_path):
    store = ce.EventStore(tmp_path / "e.sqlite3")
    assert store.append("settled", purchase_id="p1", amount_atomic=2000) is not None
    assert store.append("settled", purchase_id="p1", amount_atomic=2000) is None  # duplicate
    store.append("payment_attempt", purchase_id="p1")
    store.append("payment_attempt", purchase_id="p1")
    attempts = store.rows("SELECT attempt FROM events WHERE type='payment_attempt'")
    assert [a["attempt"] for a in attempts] == [1, 2]
    with pytest.raises(ValueError):
        store.append("bogus")
    with pytest.raises(ValueError):
        store.append("payment_attempt", url="https://x")  # fields outside the schema


def test_a_storage_failure_is_counted_then_recorded_as_a_gap(tmp_path, monkeypatch):
    store = ce.EventStore(tmp_path / "e.sqlite3")
    real = store.append
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real(*a, **k)

    monkeypatch.setattr(store, "append", flaky)
    store.record("payment_attempt", purchase_id="p1")  # lost
    store.record("payment_attempt", purchase_id="p2")  # lost (gap write fails too)
    assert store.lost == 2 and store.last_error == "OperationalError"
    store.record("payment_attempt", purchase_id="p3")
    rows = store.rows("SELECT type, detail FROM events ORDER BY seq")
    assert rows[0]["type"] == "telemetry_gap" and json.loads(rows[0]["detail"])["lost"] == 2
    assert store.lost == 0 and store.health()["gaps_recorded"]["lost"] == 2


def test_concurrent_writers_keep_one_valid_chain(tmp_path):
    store = ce.EventStore(tmp_path / "e.sqlite3")

    def write(i: int) -> None:
        for j in range(50):
            store.record("payment_attempt", purchase_id=f"p{i}-{j}")

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.lost == 0
    assert store.verify_chain() == {"ok": True, "checked": 400, "first_bad_seq": None}


def test_history_survives_a_restart_and_the_chain_continues(tmp_path):
    path = tmp_path / "e.sqlite3"
    first = ce.EventStore(path)
    first.append("settled", purchase_id="p1", amount_atomic=2000)
    head = first.head()
    first._db.close()
    second = ce.EventStore(path)  # a new process after a redeploy, same disk
    assert second.head() == head
    second.append("payment_attempt", purchase_id="p2")
    assert second.verify_chain()["ok"] and second.verify_chain()["checked"] == 2


# -- the paid path: x402 ---------------------------------------------------------------------------


def test_a_paid_x402_read_records_one_purchase_with_its_settlement(tmp_path):
    c, store, settings = _x402_app(tmp_path)
    header = _sign(c, "https://news.test/a")
    r = c.post(
        "/v1/page", json={"url": "https://news.test/a"}, headers={"PAYMENT-SIGNATURE": header}
    )
    assert r.status_code == 200
    (pid,) = {e["purchase_id"] for e in store.rows("SELECT purchase_id FROM events")}
    assert _types(store, pid) == [
        "payment_attempt", "payment_verified", "delivery_started", "delivered", "settled",
        "receipt_issued",
    ]  # fmt: skip
    settled = store.rows("SELECT * FROM events WHERE type='settled'")[0]
    assert settled["tx_hash"].startswith("0x") and len(settled["tx_hash"]) == 66
    assert settled["ledger_key"] == r.json()["receipt"]["payment_ref"]
    assert settled["amount_atomic"] == 2000 and settled["network"] == settings.network
    assert settled["pay_to"] == PAY_TO and settled["payer"] == settled["ledger_key"].split(":")[0]
    s = _evidence(store, settings).summary()
    assert s["money"]["settled_purchases"] == 1 and s["money"]["settled_revenue_usd"] == "0.002"
    assert s["reconciliation"]["issues"] == []


def test_a_retry_of_the_same_x402_payment_is_a_recovery_not_a_second_sale(tmp_path):
    c, store, settings = _x402_app(tmp_path)
    header = _sign(c, "https://news.test/a")
    body = {"url": "https://news.test/a", "request_id": "r1"}
    assert c.post("/v1/page", json=body, headers={"PAYMENT-SIGNATURE": header}).status_code == 200
    again = c.post("/v1/page", json=body, headers={"PAYMENT-SIGNATURE": header})
    assert again.status_code == 200 and again.json()["receipt"]["recovered"] is True
    purchases = _evidence(store, settings).purchases()
    assert len(purchases) == 1 and purchases[0]["recoveries"] == 1
    s = _evidence(store, settings).summary()
    assert s["money"]["settled_purchases"] == 1 and s["money"]["settled_revenue_usd"] == "0.002"
    assert s["intent"]["recoveries"] == 1


def test_an_x402_settlement_failure_is_recorded_and_never_counted_as_revenue(tmp_path):
    fac = FakeFacilitator(settle_outcomes=[settle_failure()])
    c, store, settings = _x402_app(tmp_path, fac=fac)
    header = _sign(c, "https://news.test/a")
    r = c.post(
        "/v1/page", json={"url": "https://news.test/a"}, headers={"PAYMENT-SIGNATURE": header}
    )
    assert r.status_code == 402
    (p,) = _evidence(store, settings).purchases()
    assert p["status"] == "settlement_failed" and p["delivered"] is True
    assert any(code.startswith("x402:") for code in p["codes"])
    assert _evidence(store, settings).summary()["money"]["settled_revenue_usd"] == "0"


def test_a_refused_read_is_a_delivery_failure_and_a_price_check_is_only_counted(tmp_path):
    c, store, settings = _x402_app(tmp_path)
    header = _sign(c, "https://news.test/missing")
    r = c.post("/v1/page", json={"url": "https://news.test/missing"},
               headers={"PAYMENT-SIGNATURE": header})  # fmt: skip
    assert r.status_code == 422
    (p,) = _evidence(store, settings).purchases()
    assert p["status"] == "delivery_failed"
    hours = store.rows("SELECT SUM(n) AS n FROM challenges")
    assert hours[0]["n"] == 1  # the unpaid 402 before signing
    assert "challenge" not in _types(store)


def test_recording_changes_no_response_or_ledger_row(tmp_path):
    """The same purchases with the recorder on, off, and failing on every write."""

    class Failing(ce.EventStore):
        def append(self, *a, **k):
            raise sqlite3.OperationalError("disk full")

    results = {}
    for name, recorder in (("on", "store"), ("off", ce.NullRecorder()),
                           ("failing", Failing(tmp_path / "f" / "e.sqlite3"))):  # fmt: skip
        d = tmp_path / name
        c, store, _ = _x402_app(d, recorder=recorder)
        out = []
        for url in ("https://news.test/a", "https://news.test/missing"):
            h = _sign(c, url)
            r = c.post("/v1/page", json={"url": url}, headers={"PAYMENT-SIGNATURE": h})
            body = r.json()
            body.pop("fetched_at", None)
            body.get("receipt", {}).pop("payment_ref", None)  # differs per signature
            out.append((r.status_code, body))
        stats = c.get("/v1/operator/stats", headers={"Authorization": "Bearer op"}).json()
        ledger = sqlite3.connect(d / "web_read.sqlite3").execute(
            "SELECT url, bytes, words, sha256, price_usd, settled FROM reads").fetchall()  # fmt: skip  # noqa: E501
        results[name] = (out, stats["settled_reads"], stats["revenue_usd"], stats["funnel"], ledger)
    assert results["on"] == results["off"] == results["failing"]


# -- the paid path: MPP ----------------------------------------------------------------------------


class Rail:
    def __init__(self, settle: str = "ok", same_credential: bool = False):
        self.mode, self.same, self.n = settle, same_credential, 0

    async def challenge_headers(self, url: str, method: str = "POST"):
        return [("WWW-Authenticate", 'Payment id="x", method="tempo", intent="charge"')]

    async def validate(self, authorization: str, url: str | None = None):
        self.n += 1
        return SimpleNamespace(payload={"type": "transaction", "n": 1 if self.same else self.n}), \
            PAYER, "transaction"  # fmt: skip

    async def settle(self, credential):
        if self.mode == "fail":
            raise RuntimeError("broadcast failed")
        return SimpleNamespace(reference="0x" + "b" * 64, payer=PAYER,
                               to_payment_receipt=lambda: "receipt")  # fmt: skip


def _mpp_app(tmp_path: Path, rail: Rail):
    settings = _settings(tmp_path)
    reader = fetch.Fetcher(settings.user_agent, transport=_site(), resolve=_dns)
    store = ce.EventStore(tmp_path / "commerce_events.sqlite3")
    app = svc.create_app(settings, facilitator=FakeFacilitator(), fetcher=reader, tempo_rail=rail,
                         recorder=store)  # fmt: skip
    return TestClient(app), store, settings


PAY = {"Authorization": "Payment eyJjcmVkZW50aWFsIjoieCJ9"}


def test_a_paid_mpp_read_records_its_transaction_hash_and_maps_retries_to_it(tmp_path):
    c, store, settings = _mpp_app(tmp_path, Rail(same_credential=True))
    body = {"url": "https://news.test/a", "request_id": "m1"}
    assert c.post("/v1/page", json=body, headers=PAY).status_code == 200
    again = c.post("/v1/page", json=body, headers=PAY)  # the same credential, after a lost reply
    assert again.status_code == 200
    (p,) = _evidence(store, settings).purchases()
    assert p["status"] == "settled" and p["rail"] == "mpp"
    assert p["tx_hash"] == "0x" + "b" * 64 and p["ledger_key"] == "tempo:0x" + "b" * 64
    assert p["recoveries"] == 1 and p["attempts"] == 2 and p["retries"] == 1
    assert _evidence(store, settings).summary()["money"]["settled_revenue_usd"] == "0.002"


def test_an_mpp_settlement_failure_is_recorded(tmp_path):
    c, store, settings = _mpp_app(tmp_path, Rail(settle="fail"))
    assert c.post("/v1/page", json={"url": "https://news.test/a"}, headers=PAY).status_code == 402
    (p,) = _evidence(store, settings).purchases()
    assert p["status"] == "settlement_failed"
    assert _evidence(store, settings).summary()["money"]["settled_purchases"] == 0


# -- historical backfill, classification and the chain check --------------------------------------

T1 = "0x" + "1" * 64
NONCE = "0x" + "2" * 64
PAYER2 = "0x" + "c" * 40


def _historical_ledger(tmp_path: Path):
    ledger = svc.Ledger(tmp_path / "web_read.sqlite3")
    ledger.delivered(f"tempo:{T1}", PAYER, (None, None, 10, 2, "h"), "0.002", "i1", None)
    ledger.settled(f"tempo:{T1}")
    ledger.delivered(f"{PAYER2}:{NONCE}", PAYER2, (None, None, 10, 2, "h"), "0.002", "i2", None)
    ledger.settled(f"{PAYER2}:{NONCE}")
    return _settings(tmp_path)


def test_historical_sales_are_imported_once_with_their_provenance(tmp_path):
    settings = _historical_ledger(tmp_path)
    store = ce.EventStore(tmp_path / "commerce_events.sqlite3")
    ev = _evidence(store, settings, payer_classes={PAYER: "independent"})
    assert ev.backfill() == 4  # delivered + settled for each
    assert ev.backfill() == 0  # idempotent
    s = ev.summary()
    assert s["money"]["settled_purchases"] == 2 and s["money"]["settled_revenue_usd"] == "0.004"
    assert s["buyers"] == {"paying_wallets": 2, "independent_confirmed": 1, "unconfirmed": 1,
                           "affiliated_or_test": 0, "repeat_buyers": 0}  # fmt: skip
    by_rail = {p["rail"]: p for p in ev.purchases()}
    assert by_rail["mpp"]["tx_hash"] == T1 and by_rail["mpp"]["provenance"] == "ledger_backfill"
    assert by_rail["x402"]["tx_hash"] is None  # not recorded before the recorder existed
    assert by_rail["x402"]["attempts"] == 0  # earlier attempts are not invented
    detail = json.loads(ev.timeline(by_rail["x402"]["purchase_id"])[0]["detail"])
    assert "earlier attempts and rejections" in detail["not_recoverable"]
    assert s["reconciliation"]["issues"] == []


def test_a_live_sale_is_never_duplicated_by_the_backfill(tmp_path):
    c, store, settings = _x402_app(tmp_path)
    h = _sign(c, "https://news.test/a")
    c.post("/v1/page", json={"url": "https://news.test/a"}, headers={"PAYMENT-SIGNATURE": h})
    ev = _evidence(store, settings)
    assert ev.backfill() == 0
    assert ev.summary()["money"]["settled_purchases"] == 1


def _rpc(receipts: dict[str, dict]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        tx = json.loads(request.content)["params"][0]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": receipts.get(tx)})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:]


def _receipt(payer: str, amount: int, nonce: str | None = None) -> dict:
    logs = [{"topics": [gmod.TRANSFER, _topic(payer), _topic(PAY_TO)], "data": hex(amount)}]
    if nonce:
        logs.append({"topics": [gmod.AUTHORIZATION_USED, _topic(payer), nonce], "data": "0x"})
    return {"status": "0x1", "blockNumber": "0x10", "logs": logs}


def test_the_chain_check_confirms_hints_and_flags_mismatches(tmp_path):
    settings = _historical_ledger(tmp_path)
    store = ce.EventStore(tmp_path / "commerce_events.sqlite3")
    t2 = "0x" + "3" * 64
    rpcs = {"tempo:4217": "https://rpc.test/tempo", settings.network: "https://rpc.test/base"}
    ev = _evidence(store, settings, chain_check=True, rpcs=rpcs,
                   tx_hints={f"{PAYER2}:{NONCE}": t2})  # fmt: skip
    ev.backfill()
    client = _rpc({T1: _receipt(PAYER, 2000), t2: _receipt(PAYER2, 2000, NONCE)})
    assert asyncio.run(ev.chain_check(client)) == 2
    outcomes = {p["rail"]: p["chain"]["outcome"] for p in ev.purchases()}
    assert outcomes == {"mpp": "chain_confirmed", "x402": "chain_confirmed"}
    # A receipt that pays the wrong amount is a mismatch and shows up in reconciliation.
    store2 = ce.EventStore(tmp_path / "b" / "e.sqlite3")
    ev2 = _evidence(store2, settings, chain_check=True, rpcs=rpcs)
    ev2.backfill()
    asyncio.run(ev2.chain_check(_rpc({T1: _receipt(PAYER, 1999)})))
    assert any(i["kind"] == "chain_mismatch" for i in ev2.summary()["reconciliation"]["issues"])


def test_an_unreachable_rpc_records_nothing_and_is_retried_later(tmp_path):
    settings = _historical_ledger(tmp_path)
    store = ce.EventStore(tmp_path / "commerce_events.sqlite3")
    ev = _evidence(store, settings, chain_check=True, rpcs={"tempo:4217": "https://rpc.test"})
    ev.backfill()

    def down(request):
        raise httpx.ConnectError("down")

    asyncio.run(ev.chain_check(httpx.AsyncClient(transport=httpx.MockTransport(down))))
    assert store.rows("SELECT * FROM events WHERE type='reconciliation'") == []


def test_a_ledger_sale_missing_from_the_history_is_a_discrepancy(tmp_path):
    settings = _historical_ledger(tmp_path)
    store = ce.EventStore(tmp_path / "commerce_events.sqlite3")
    store.append("settled", purchase_id="ghost", ledger_key="0xdead:0x01", amount_atomic=2000)
    issues = _evidence(store, settings).summary()["reconciliation"]["issues"]
    kinds = {i["kind"] for i in issues}
    assert {"ledger_sale_without_event", "event_sale_without_ledger_row"} <= kinds


# -- the private view -----------------------------------------------------------------------------


def _ops(tmp_path: Path, **gm):
    settings = _historical_ledger(tmp_path)
    store = ce.EventStore(tmp_path / "commerce_events.sqlite3")
    g = gmod.GodModeSettings(login_secret=SECRET, payer_classes={PAYER: "independent"}, **gm)
    app = gmod.create_app(store, tmp_path / "web_read.sqlite3", settings, g)
    from starlette.applications import Starlette
    from starlette.routing import Mount

    outer = Starlette(routes=[Mount("/ops", app=app)])
    return TestClient(outer, base_url="https://ops.test"), store


def _login(c: TestClient, secret: str = SECRET):
    form = c.get("/ops/login")
    token = c.cookies.get("gm_csrf")
    assert token and 'name="csrf"' in form.text
    return c.post("/ops/login", data={"secret": secret, "csrf": token}, follow_redirects=False)


def test_everything_needs_a_session(tmp_path):
    c, _ = _ops(tmp_path)
    for path in ("/ops/", "/ops/transactions", "/ops/reconciliation", "/ops/export"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/ops/login"
    for path in ("/ops/export.json", "/ops/api/summary"):
        assert c.get(path).status_code == 401
    assert c.get("/ops/api/archive").status_code == 404  # no archive token configured
    nope = c.get("/ops/nothing-here")
    assert nope.status_code == 404 and nope.headers["cache-control"] == "no-store"


def test_sign_in_sets_a_strict_session_and_every_page_is_private(tmp_path):
    c, store = _ops(tmp_path)
    r = _login(c)
    assert r.status_code == 303 and r.headers["location"] == "/ops/"
    cookie = r.headers["set-cookie"].lower()
    for flag in ("httponly", "secure", "samesite=strict", "path=/ops"):
        assert flag in cookie
    page = c.get("/ops/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "noindex" in page.headers["x-robots-tag"]
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    # Browsers send `Origin: null` on form posts under no-referrer; sign-in would always fail.
    assert page.headers["referrer-policy"] == "same-origin"
    assert "<script>" not in page.text and SECRET not in page.text
    text = page.text
    assert "$0.004" in text and "Independence unconfirmed" in text
    assert "not, by themselves, evidence of repeatable demand" in text
    assert store.rows("SELECT kind, ok FROM access_log") == [{"kind": "login", "ok": 1}]


def test_bad_secrets_forged_cookies_and_missing_csrf_are_refused(tmp_path):
    c, store = _ops(tmp_path)
    assert _login(c, "wrong" * 10).status_code == 401
    token = c.cookies.get("gm_csrf")
    no_csrf = c.post("/ops/login", data={"secret": SECRET}, follow_redirects=False)
    assert no_csrf.status_code == 401
    null = c.post(
        "/ops/login",
        data={"secret": SECRET, "csrf": token},
        headers={"origin": "null"},
        follow_redirects=False,
    )
    assert null.status_code == 401
    cross = c.post("/ops/login", data={"secret": SECRET, "csrf": token},
                   headers={"origin": "https://evil.test"}, follow_redirects=False)  # fmt: skip
    assert cross.status_code == 401
    c.cookies.set("gm_session", "eyJleHAiOjk5OTk5OTk5OTl9.forged", domain="ops.test", path="/ops")
    assert c.get("/ops/", follow_redirects=False).status_code == 303


def test_failed_sign_ins_are_throttled(tmp_path):
    c, store = _ops(tmp_path)
    for _ in range(gmod.FAILS_PER_SOURCE):
        assert _login(c, "x" * 40).status_code == 401
    assert _login(c).status_code == 429  # even the right secret waits out the window
    assert store.access_failures_since(time.time() - 60) == gmod.FAILS_PER_SOURCE + 1


def test_an_expired_session_is_refused_and_logout_needs_its_token(tmp_path):
    c, _ = _ops(tmp_path)
    _login(c)
    assert c.post("/ops/logout", data={"csrf": "x"}, follow_redirects=False).status_code == 403
    page = c.get("/ops/")
    token = page.text.split('name="csrf" value="')[1].split('"')[0]
    out = c.post("/ops/logout", data={"csrf": token}, follow_redirects=False)
    assert out.status_code == 303
    c2, store = _ops(tmp_path / "x")
    auth = gmod.Auth(gmod.GodModeSettings(login_secret=SECRET, session_s=-1), store)
    c2.cookies.set("gm_session", auth.issue(), domain="ops.test", path="/ops")
    assert c2.get("/ops/", follow_redirects=False).status_code == 303


def test_the_explorer_filters_and_the_detail_timeline(tmp_path):
    c, _ = _ops(tmp_path)
    _login(c)
    page = c.get("/ops/transactions?rail=mpp")
    assert page.status_code == 200 and "1 of 2 purchase" in page.text
    pid = page.text.split('href="/ops/transactions/')[1].split('"')[0]
    detail = c.get(f"/ops/transactions/{pid}")
    assert detail.status_code == 200
    assert (
        "reconstructed from the ledger" in detail.text
        and "https://explore.tempo.xyz/tx/" in detail.text
    )
    assert c.get("/ops/transactions/unknown").status_code == 404
    assert c.get("/ops/transactions?q=" + T1[:10]).text.count('href="/ops/transactions/') == 1
    assert c.get("/ops/static/app.js").headers["content-type"].startswith("text/javascript")


def test_the_investor_export_is_sanitized_and_states_its_limits(tmp_path):
    c, _ = _ops(tmp_path)
    _login(c)
    x = c.get("/ops/export.json")
    assert x.status_code == 200 and "attachment" in x.headers["content-disposition"]
    data = x.json()
    assert data["totals"] == {"settled_purchases": 2, "settled_revenue_usd": "0.004",
                              "independent_buyers_confirmed": 1,
                              "buyers_not_confirmed_independent": 1, "repeat_buyers": 0}  # fmt: skip  # noqa: E501
    text = json.dumps(data).lower()
    assert PAYER not in text and PAYER2 not in text  # no wallet addresses
    assert "product-market fit" in text
    buyers = sorted(p["buyer"] for p in data["purchases"])
    assert buyers == ["independence not confirmed", "independent (operator-confirmed)"]


def test_the_archive_pull_needs_its_own_token_and_pages_by_sequence(tmp_path):
    c, store = _ops(tmp_path, archive_token=ARCHIVE)
    _login(c)
    c.get("/ops/")  # runs the backfill
    assert c.get("/ops/api/archive").status_code == 401  # a session is not the archive token
    hdr = {"Authorization": f"Bearer {ARCHIVE}"}
    first = c.get("/ops/api/archive?after=0&limit=3", headers=hdr).json()
    assert len(first["events"]) == 3 and first["head"]["seq"] == 4
    rest = c.get(f"/ops/api/archive?after={first['next_after']}", headers=hdr).json()
    assert [e["seq"] for e in rest["events"]] == [4]
    # The pulled events re-verify off-host from the genesis hash.
    prev = ce.GENESIS
    for e in first["events"] + rest["events"]:
        assert e["prev_hash"] == prev and ce.chain_hash(prev, e) == e["hash"]
        prev = e["hash"]


def test_no_private_request_content_reaches_the_history_or_the_pages(tmp_path):
    c, store, settings = _x402_app(tmp_path)
    h = _sign(c, PRIVATE_URL)
    body = {"url": PRIVATE_URL, "request_id": "private-req-77"}
    assert c.post("/v1/page", json=body, headers={"PAYMENT-SIGNATURE": h}).status_code == 200
    raw = (tmp_path / "commerce_events.sqlite3").read_bytes()
    for marker in (b"secret-path-zq9", b"private-req-77", h[:40].encode()):
        assert marker not in raw
    g = gmod.GodModeSettings(login_secret=SECRET)
    app = gmod.create_app(store, tmp_path / "web_read.sqlite3", settings, g)
    from starlette.applications import Starlette
    from starlette.routing import Mount

    ops = TestClient(Starlette(routes=[Mount("/ops", app=app)]), base_url="https://ops.test")
    _login(ops)
    for path in ("/ops/", "/ops/transactions", "/ops/export.json"):
        assert "secret-path-zq9" not in ops.get(path).text


def test_settings_refuse_weak_or_malformed_configuration():
    assert gmod.GodModeSettings.from_env({}) is None
    with pytest.raises(RuntimeError):
        gmod.GodModeSettings.from_env({"GODMODE_LOGIN_SECRET": "short"})
    with pytest.raises(RuntimeError):
        gmod.GodModeSettings.from_env(
            {"GODMODE_LOGIN_SECRET": SECRET, "GODMODE_ARCHIVE_TOKEN": SECRET}
        )
    with pytest.raises(RuntimeError):
        gmod.GodModeSettings.from_env({"GODMODE_LOGIN_SECRET": SECRET,
                                       "GODMODE_PAYER_CLASSES": f"{PAYER}=vip"})  # fmt: skip
    g = gmod.GodModeSettings.from_env({
        "GODMODE_LOGIN_SECRET": SECRET, "GODMODE_CHAIN_CHECK": "1",
        "GODMODE_PAYER_CLASSES": f"{PAYER.upper()}=independent",
    })  # fmt: skip
    assert g is not None and g.chain_check and g.payer_classes == {PAYER: "independent"}


# -- mounted in the production origin --------------------------------------------------------------


def _cohost():
    return _load("bg_cohost_godmode", ROOT / "hosted" / "budget_gateway" / "cohost.py")


def test_ops_is_mounted_only_with_its_secret_and_the_recorder(tmp_path):
    cohost = _cohost()
    settings = _settings(tmp_path)
    reader = fetch.Fetcher(settings.user_agent, transport=_site(), resolve=_dns)
    plain = svc.create_app(settings, facilitator=FakeFacilitator(), fetcher=reader)
    assert cohost.godmode_from_env((plain, settings), {}) is None  # no secret: nothing mounted
    no_recorder = cohost.godmode_from_env((plain, settings), {"GODMODE_LOGIN_SECRET": SECRET})
    assert no_recorder is None  # the recorder is off: refused, logged, not mounted
    store = ce.EventStore(tmp_path / "commerce_events.sqlite3")
    recording = svc.create_app(settings, facilitator=FakeFacilitator(), fetcher=reader,
                               recorder=store)  # fmt: skip
    weak = cohost.godmode_from_env((recording, settings), {"GODMODE_LOGIN_SECRET": "short"})
    assert weak is None
    assert cohost.godmode_from_env((recording, settings), {"GODMODE_LOGIN_SECRET": SECRET})


def test_recording_is_off_by_default():
    env = {"READ_PAY_TO": PAY_TO, "READ_RESOURCE_URL": "http://testserver/v1/page"}
    assert svc.Settings.from_env(env).commerce_events is False
    assert svc.Settings.from_env({**env, "READ_COMMERCE_EVENTS": "1"}).commerce_events is True


def test_a_malformed_settlement_receipt_never_withholds_a_paid_result(tmp_path):
    class OddRail(Rail):
        async def settle(self, credential):
            return SimpleNamespace(
                reference="0x" + "d" * 64, payer=12345, to_payment_receipt=lambda: "receipt"
            )

    c, store, _ = _mpp_app(tmp_path, OddRail())
    r = c.post("/v1/page", json={"url": "https://news.test/a"}, headers=PAY)
    assert r.status_code == 200 and r.json()["content_markdown"]
