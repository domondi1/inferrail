"""Deterministic failure-injection tests for the Phase C payment-settlement
boundary repair (`POST /sessions`).

These tests exercise the REAL x402 SDK lifecycle end-to-end through
`server.build_app` and a real `fastapi.testclient.TestClient` -- a real
throwaway `eth_account` key signs a real, structurally valid EIP-3009
payment authorization via the real `x402ClientSync`/`ExactEvmScheme`, and
that signature is verified for real against CDP's Base Sepolia facilitator
(this is free and requires no funded wallet -- verification checks the
signature and requirements, never a balance). Only `HTTPFacilitatorClient
.settle` -- the one step that would otherwise require a genuinely funded
wallet and a real on-chain transfer -- is monkeypatched, so the exact point
of failure (or success) is deterministic and never depends on real network
conditions, gas prices, or wallet balances.

Skips automatically unless the hosted extra (fastapi/cdp-sdk/x402) is
installed AND CDP + seller-address env vars are set, exactly like
`test_a2a_economic_authority_session_service.py`. Never performs a real
payment or touches mainnet.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTED_DIR = REPO_ROOT / "hosted" / "a2a_economic_authority"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

import pytest  # noqa: E402

pytest.importorskip("a2a")
pytest.importorskip("cdp")
pytest.importorskip("x402")
pytest.importorskip("eth_account")

_HAVE_ENV = bool(
    os.environ.get("CDP_API_KEY_ID")
    and os.environ.get("CDP_API_KEY_SECRET")
    and os.environ.get("ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS")
)

pytestmark = pytest.mark.skipif(
    not _HAVE_ENV,
    reason="requires CDP_API_KEY_ID/CDP_API_KEY_SECRET/ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS",
)

import hashlib  # noqa: E402
import secrets  # noqa: E402

import fastapi  # noqa: E402
import server  # noqa: E402
from capabilities import CapabilityStore  # noqa: E402
from core import EconomicAuthorityStore  # noqa: E402
from eth_account import Account  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from x402.client import x402ClientSync  # noqa: E402
from x402.http.utils import encode_payment_signature_header  # noqa: E402
from x402.mechanisms.evm.exact import ExactEvmScheme  # noqa: E402
from x402.schemas import PaymentRequired  # noqa: E402
from x402.schemas.responses import SettleResponse, VerifyResponse  # noqa: E402

# `fastapi` and `VerifyResponse` are imported at module level, not inside
# the test that uses them for its inline standalone route, because this
# file's `from __future__ import annotations` makes FastAPI resolve
# route-handler type hints as strings against the function's *module*
# globals -- a handler defined inside a test function can still see them
# there, but not if they were only ever imported into that test's local
# scope. Getting this wrong makes FastAPI silently treat a `Request`
# parameter as a missing query parameter instead of the special injected
# request object (a real gotcha independent of this repair).


@pytest.fixture
def stores(tmp_path):
    db_path = tmp_path / "authority.sqlite3"
    capability_db_path = tmp_path / "capabilities.sqlite3"
    app = server.build_app(
        base_url="http://testserver/", db_path=db_path, capability_db_path=capability_db_path
    )
    client = TestClient(app)
    core_store = EconomicAuthorityStore(db_path)
    capability_store = CapabilityStore(capability_db_path)
    return client, core_store, capability_store


def _unpaid_402(client: TestClient, body: dict) -> PaymentRequired:
    resp = client.post("/sessions", json=body)
    assert resp.status_code == 402
    encoded = resp.headers["payment-required"]
    return PaymentRequired.model_validate(json.loads(base64.b64decode(encoded)))


def _nonce_from_header(header: str) -> str:
    """Extracts `payment_nonce` straight from the signed header the buyer
    itself built -- exactly the information a real buyer's client already
    holds before ever sending the request (see
    `x402.mechanisms.evm.utils.create_nonce`, called client-side). Used by
    recovery tests to prove recovery never needs `session_id`."""
    from x402.http.utils import decode_payment_signature_header

    payload = decode_payment_signature_header(header)
    authorization = payload.payload["authorization"]  # type: ignore[index]
    return str(authorization["nonce"])


def _signed_header(payment_required: PaymentRequired) -> tuple[str, str]:
    """Signs a real (throwaway, unfunded) EIP-3009 authorization against
    the server's own advertised requirements, exactly as a real buyer's
    client library would -- proving the signature genuinely verifies,
    without needing a funded wallet (verification checks the signature,
    never a balance; only settlement, which these tests control
    deterministically, would need real funds)."""
    account = Account.create()
    buyer = x402ClientSync()
    buyer.register("eip155:84532", ExactEvmScheme(signer=account))
    payload = buyer.create_payment_payload(payment_required)
    return encode_payment_signature_header(payload), account.address


def _session_row_count(capability_store: CapabilityStore) -> int:
    with sqlite3.connect(capability_store.db_path) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM session_purchases").fetchone()
        return count


def _live_token_count(capability_store: CapabilityStore, session_id: str) -> int:
    with sqlite3.connect(capability_store.db_path) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM capability_tokens WHERE delegation_id = ? AND revoked = 0",
            (session_id,),
        ).fetchone()
        return count


def _fake_settle(success: bool, **overrides):
    async def _settle(self, payload, requirements):  # noqa: ANN001
        fields = {
            "success": success,
            "transaction": "" if not success else "0x" + secrets.token_hex(32),
            "network": requirements.network,
        }
        if not success:
            fields["error_reason"] = overrides.pop("error_reason", "insufficient_funds")
            fields["error_message"] = overrides.pop("error_message", "injected test failure")
        fields.update(overrides)
        return SettleResponse(**fields)

    return _settle


# -- 1. verification succeeds, settlement fails -----------------------------


def test_settlement_failure_leaves_no_session_and_no_credential(stores, monkeypatch):
    """The core repair guarantee: under the route's "upfront" payment flow,
    a failed settlement means the route handler never ran at all -- no
    durable session row, no root credential, and the client sees a plain
    402, never a 200 with `status: "PAID"`."""
    client, _core, capability_store = stores
    monkeypatch.setattr(server.HTTPFacilitatorClient, "settle", _fake_settle(False))

    body = {
        "agent_id": "buyer-settle-fail",
        "authority_ceiling_usd": "10.00",
        "recovery_secret_hash": _recovery_secret_pair()[1],
    }
    payment_required = _unpaid_402(client, body)
    header, _payer = _signed_header(payment_required)

    handler_calls = []
    real_handler = server.handle_session_request

    def spy(*args, **kwargs):
        handler_calls.append((args, kwargs))
        return real_handler(*args, **kwargs)

    monkeypatch.setattr(server, "handle_session_request", spy)

    resp = client.post("/sessions", json=body, headers={"PAYMENT-SIGNATURE": header})

    assert resp.status_code in (402, 500, 502), resp.text
    assert "PAID" not in resp.text
    assert "root_capability" not in resp.text
    assert len(handler_calls) == 0, "the route handler must never run when settlement fails"
    assert _session_row_count(capability_store) == 0, (
        "a failed settlement must never leave a durable session behind"
    )


# -- 2. characterizes the historical defect this repair closes --------------


def test_reproduces_the_pre_repair_defect_when_settlement_runs_after_the_handler(
    stores, monkeypatch
):
    """Builds a second, standalone route -- deliberately using the exact-evm
    scheme's DEFAULT `"authorization"` payment flow (settle after handler),
    which is what `/sessions` used before this repair -- wired to the exact
    same `sessions.handle_session_request` business logic. Proves the
    historical defect this repair closes: a settlement failure AFTER a
    successful verification still leaves a durably-created session and an
    unclaimed root credential behind, even though the buyer-visible response
    is a 402 (the middleware discards the handler's 200 and replaces it).
    This route is never wired into `server.build_app` -- it exists only in
    this test, to document the defect without reintroducing it."""
    from decimal import Decimal

    from capabilities import CapabilityStore as _CapabilityStore
    from cdp.x402 import create_facilitator_config
    from core import EconomicAuthorityStore as _EconomicAuthorityStore
    from sessions import handle_session_request
    from x402.http import HTTPFacilitatorClient
    from x402.http.middleware.fastapi import payment_middleware
    from x402.http.types import PaymentOption, RouteConfig
    from x402.mechanisms.evm.exact import register_exact_evm_server
    from x402.server import x402ResourceServer

    pay_to = os.environ["ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS"]
    core_db = "/tmp/_defect_repro_core.sqlite3"
    cap_db = "/tmp/_defect_repro_cap.sqlite3"
    for p in (core_db, cap_db):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    core_store = _EconomicAuthorityStore(core_db)
    capability_store = _CapabilityStore(cap_db)

    app = fastapi.FastAPI()
    facilitator_config = create_facilitator_config(
        api_key_id=os.environ["CDP_API_KEY_ID"], api_key_secret=os.environ["CDP_API_KEY_SECRET"]
    )
    x402_server = x402ResourceServer(HTTPFacilitatorClient(facilitator_config))
    register_exact_evm_server(x402_server, networks="eip155:84532")
    routes = {
        "POST /pre-repair-sessions": RouteConfig(
            accepts=PaymentOption(
                scheme="exact",
                pay_to=pay_to,
                price="$0.05",
                network="eip155:84532",
                # Deliberately NOT extra={"paymentFlow": "upfront"} -- this
                # is the scheme's default flow, settling AFTER the handler.
            ),
            resource="http://testserver/pre-repair-sessions",
            service_name="Pre-repair repro",
        )
    }
    x402_mw = payment_middleware(routes, x402_server)

    @app.middleware("http")
    async def _mw(request, call_next):  # noqa: ANN001
        return await x402_mw(request, call_next)

    @app.post("/pre-repair-sessions")
    async def pre_repair_create(request: fastapi.Request):
        body = await request.json()
        payment_payload = request.state.payment_payload
        # Correct (dict-based) nonce extraction -- isolates this test to
        # ONLY the settlement-ordering defect, not the separate
        # nonce-extraction bug this repair also fixes in server.py.
        nonce = payment_payload.payload["authorization"]["nonce"]
        status_code, response_body = handle_session_request(
            core_store, capability_store, body=body, payment_nonce=nonce,
            service_fee_usd=Decimal("0.05"),
        )
        return JSONResponse(response_body, status_code=status_code)

    client = TestClient(app)
    # This flow's `verify_before_handler=True` calls the REAL facilitator,
    # which simulates the on-chain transfer and genuinely reverts for an
    # unfunded throwaway signer -- a real "insufficient funds" rejection,
    # not the defect under test. Force verification to succeed so this
    # test isolates exactly one thing: what the ordering defect does once
    # verification has already passed. `.settle` is what this test is
    # actually about, so it stays deterministically failing.
    async def _fake_verify(self, payload, requirements):  # noqa: ANN001
        return VerifyResponse(is_valid=True, payer=payload.payload["authorization"]["from"])

    monkeypatch.setattr(server.HTTPFacilitatorClient, "verify", _fake_verify)
    monkeypatch.setattr(server.HTTPFacilitatorClient, "settle", _fake_settle(False))

    body = {
        "agent_id": "buyer-defect-repro",
        "authority_ceiling_usd": "10.00",
        "recovery_secret_hash": _recovery_secret_pair()[1],
    }
    resp = client.post("/pre-repair-sessions", json=body)
    assert resp.status_code == 402
    payment_required = PaymentRequired.model_validate(
        json.loads(base64.b64decode(resp.headers["payment-required"]))
    )
    header, _payer = _signed_header(payment_required)

    resp2 = client.post(
        "/pre-repair-sessions", json=body, headers={"PAYMENT-SIGNATURE": header}
    )

    # The buyer-visible response is honestly a failure (never a 200)...
    assert resp2.status_code != 200, resp2.text
    # ...but the handler already ran and committed durable state before
    # settlement was even attempted -- the defect. This is what the
    # `"upfront"` flow (test 1, above) closes structurally.
    assert _session_row_count(capability_store) == 1, (
        "pre-repair flow: the handler ran and created a session despite "
        "settlement later failing -- this is the defect being documented"
    )


# -- 3. settlement succeeds -> honestly PAID, and recovery works ------------


def _recovery_secret_pair() -> tuple[str, str]:
    plaintext = secrets.token_urlsafe(32)
    return plaintext, hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def test_settlement_success_is_honestly_paid_and_lost_response_is_recoverable(
    stores, monkeypatch
):
    """Recovery is keyed on `payment_nonce`, not the server-generated
    `session_id` -- so this test deliberately proves recovery using ONLY
    `payment_nonce` (extracted straight from the signed header the buyer
    itself built, exactly as the buyer's own client already has it) and
    the buyer's own `recovery_secret`, and never reads `session_id` out
    of `data` before calling `/sessions/recover`. This is the actual
    "every response lost" scenario: a buyer who genuinely never received
    `resp` never learns `session_id` at all, so recovery MUST NOT require
    it."""
    client, core_store, capability_store = stores
    monkeypatch.setattr(server.HTTPFacilitatorClient, "settle", _fake_settle(True))

    recovery_secret, recovery_hash = _recovery_secret_pair()
    body = {
        "agent_id": "buyer-recoverable",
        "authority_ceiling_usd": "25.00",
        "recovery_secret_hash": recovery_hash,
    }
    payment_required = _unpaid_402(client, body)
    header, _payer = _signed_header(payment_required)
    payment_nonce = _nonce_from_header(header)

    resp = client.post("/sessions", json=body, headers={"PAYMENT-SIGNATURE": header})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["service_fee"]["status"] == "PAID"
    assert data["root_capability"]["token"] is not None
    session_id = data["session_id"]
    original_token = data["root_capability"]["token"]

    # Simulate: the buyer never actually received `resp` (dropped
    # connection / crashed client) -- they only have the payment_nonce and
    # recovery_secret they generated/held locally before ever sending the
    # request, never `session_id`.
    recover_resp = client.post(
        "/sessions/recover",
        json={"payment_nonce": payment_nonce, "recovery_secret": recovery_secret},
    )
    assert recover_resp.status_code == 200, recover_resp.text
    assert recover_resp.json()["session_id"] == session_id, (
        "recovery must resolve and return the buyer's own session_id, "
        "since a lost-response buyer does not know it"
    )
    new_token = recover_resp.json()["root_capability"]["token"]
    assert new_token != original_token

    # Exactly one root credential is live -- the old one was revoked, not
    # left dangling alongside the new one.
    assert _live_token_count(capability_store, session_id) == 1
    with pytest.raises(Exception):  # noqa: B017 - old token must be dead
        info = capability_store.authorize(original_token, session_id, "reserve")
        raise AssertionError(f"old token still authorizes: {info}")
    info = capability_store.authorize(new_token, session_id, "reserve")
    assert info.delegation_id == session_id

    # Recovery never changed the economics.
    root = core_store.get(session_id)
    assert root is not None
    from decimal import Decimal

    assert root.authority_usd == Decimal("25.00")


def test_recovery_rejects_the_wrong_secret(stores, monkeypatch):
    client, _core, capability_store = stores
    monkeypatch.setattr(server.HTTPFacilitatorClient, "settle", _fake_settle(True))

    recovery_secret, recovery_hash = _recovery_secret_pair()
    body = {
        "agent_id": "buyer-wrong-secret",
        "authority_ceiling_usd": "5.00",
        "recovery_secret_hash": recovery_hash,
    }
    payment_required = _unpaid_402(client, body)
    header, _payer = _signed_header(payment_required)
    payment_nonce = _nonce_from_header(header)
    resp = client.post("/sessions", json=body, headers={"PAYMENT-SIGNATURE": header})
    assert resp.status_code == 200
    session_id = resp.json()["session_id"]
    original_token = resp.json()["root_capability"]["token"]

    bad = client.post(
        "/sessions/recover",
        json={"payment_nonce": payment_nonce, "recovery_secret": "not-the-right-secret"},
    )
    assert bad.status_code == 403
    assert bad.json()["error"] == "InvalidRecoverySecret"
    # The original credential must still be the one live -- an unrelated
    # caller presenting the wrong secret must never rotate it away.
    assert _live_token_count(capability_store, session_id) == 1
    info = capability_store.authorize(original_token, session_id, "reserve")
    assert info.delegation_id == session_id


def test_recovery_rejects_an_unrelated_payment_nonce(stores, monkeypatch):
    client, _core, _capability_store = stores
    resp = client.post(
        "/sessions/recover",
        json={"payment_nonce": "never-purchased", "recovery_secret": secrets.token_urlsafe(32)},
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "InvalidRecoverySecret"


def test_purchase_without_a_recovery_secret_hash_is_rejected_before_settlement(
    stores, monkeypatch
):
    """An agent-first buyer that omits the (now-required) recovery
    commitment must be rejected with 400 -- and never even reach
    settlement (or even payment-requirements discovery), so nothing is
    ever charged for a purchase that could not have honestly promised
    recovery. See sessions.py's module docstring on why
    `recovery_secret_hash` is required, not optional, and server.py's
    `_require_recovery_secret_hash` docstring for why this check runs as
    its own middleware BEFORE the x402 payment middleware, not merely
    before the route handler -- under this route's `"upfront"` payment
    flow, settlement happens before the handler runs regardless, so a
    handler-level check alone would reject the buyer only AFTER they had
    already been charged.

    Deliberately never calls `_unpaid_402` here: that helper itself
    asserts a 402, and a body missing `recovery_secret_hash` must never
    get one -- not even the unpaid payment-requirements-discovery
    request, since a well-behaved buyer already holds the secret before
    making ANY request for this purchase."""
    client, _core, capability_store = stores
    settle_calls: list = []
    verify_calls: list = []
    real_settle = _fake_settle(True)

    async def _spy_settle(self, payload, requirements):  # noqa: ANN001
        settle_calls.append(1)
        return await real_settle(self, payload, requirements)

    real_verify = server.HTTPFacilitatorClient.verify

    async def _spy_verify(self, payload, requirements):  # noqa: ANN001
        verify_calls.append(1)
        return await real_verify(self, payload, requirements)

    monkeypatch.setattr(server.HTTPFacilitatorClient, "settle", _spy_settle)
    monkeypatch.setattr(server.HTTPFacilitatorClient, "verify", _spy_verify)

    body = {"agent_id": "buyer-no-secret", "authority_ceiling_usd": "10.00"}
    resp = client.post("/sessions", json=body)
    assert resp.status_code == 400, resp.text
    assert "recovery_secret_hash" in resp.json()["error"]
    assert not verify_calls, "must never even reach x402 verification"
    assert not settle_calls, "a rejected-for-missing-recovery-secret purchase must never settle"
    assert _session_row_count(capability_store) == 0


def test_purchase_with_a_malformed_recovery_secret_hash_is_also_rejected_before_settlement(
    stores,
):
    client, _core, capability_store = stores
    resp = client.post(
        "/sessions",
        json={
            "agent_id": "buyer-bad-secret-shape",
            "authority_ceiling_usd": "10.00",
            "recovery_secret_hash": "not-a-valid-hex-sha256",
        },
    )
    assert resp.status_code == 400, resp.text
    assert "recovery_secret_hash" in resp.json()["error"]
    assert _session_row_count(capability_store) == 0


# -- 4. ordinary retry after real settlement does not recharge/recreate -----


def test_retrying_the_same_signed_request_after_settlement_never_recharges(stores, monkeypatch):
    client, core_store, capability_store = stores
    monkeypatch.setattr(server.HTTPFacilitatorClient, "settle", _fake_settle(True))

    body = {
        "agent_id": "buyer-retry",
        "authority_ceiling_usd": "12.00",
        "recovery_secret_hash": _recovery_secret_pair()[1],
    }
    payment_required = _unpaid_402(client, body)
    header, _payer = _signed_header(payment_required)

    first = client.post("/sessions", json=body, headers={"PAYMENT-SIGNATURE": header})
    assert first.status_code == 200
    session_id = first.json()["session_id"]
    assert first.json()["newly_claimed"] is True

    second = client.post("/sessions", json=body, headers={"PAYMENT-SIGNATURE": header})
    assert second.status_code == 200, second.text
    assert second.json()["session_id"] == session_id
    assert second.json()["newly_claimed"] is False
    assert second.json()["root_capability"] is None, (
        "a retry must never re-expose the plaintext root credential"
    )

    from decimal import Decimal

    root = core_store.get(session_id)
    assert root is not None
    assert root.authority_usd == Decimal("12.00"), "authority must never double on retry"
    assert _live_token_count(capability_store, session_id) == 1
    assert _session_row_count(capability_store) == 1


# -- 5. concurrent duplicate delivery of the same payment proof -------------


def test_concurrent_duplicate_requests_mint_exactly_one_credential(stores, monkeypatch):
    client, core_store, capability_store = stores
    monkeypatch.setattr(server.HTTPFacilitatorClient, "settle", _fake_settle(True))

    body = {
        "agent_id": "buyer-concurrent",
        "authority_ceiling_usd": "8.00",
        "recovery_secret_hash": _recovery_secret_pair()[1],
    }
    payment_required = _unpaid_402(client, body)
    header, _payer = _signed_header(payment_required)

    results: list = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        results.append(
            client.post("/sessions", json=body, headers={"PAYMENT-SIGNATURE": header})
        )

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    session_ids = {r.json()["session_id"] for r in results}
    assert len(session_ids) == 1
    claimed = [r for r in results if r.json()["root_capability"] is not None]
    assert len(claimed) == 1, "exactly one concurrent racer may receive the plaintext credential"

    session_id = session_ids.pop()
    assert _live_token_count(capability_store, session_id) == 1
    from decimal import Decimal

    root = core_store.get(session_id)
    assert root is not None
    assert root.authority_usd == Decimal("8.00")


# -- 6. facilitator rejects a replayed EIP-3009 authorization (window 5) ----


def test_facilitator_rejecting_a_replayed_nonce_leaves_the_first_purchase_intact(
    stores, monkeypatch
):
    """Failure window 5: the buyer retries the ORIGINAL signed payment
    (identical PAYMENT-SIGNATURE header) after it already settled once.
    Deterministically simulates the facilitator's real behavior (per this
    PR's live Base Sepolia evidence: a reused EIP-3009 nonce is rejected
    by the facilitator itself, before application code runs) by having
    `.settle` succeed exactly once and then fail for every subsequent
    call with the same payload -- proving the retry is correctly refused
    (never a second session, never a second charge application-side) AND
    that the buyer's already-completed first purchase is completely
    unaffected by the failed retry."""
    client, core_store, capability_store = stores

    real_settle = _fake_settle(True)
    seen_nonces: set[str] = set()

    async def _settle_once_per_nonce(self, payload, requirements):  # noqa: ANN001
        nonce = payload.payload["authorization"]["nonce"]
        if nonce in seen_nonces:
            return SettleResponse(
                success=False,
                transaction="",
                network=requirements.network,
                error_reason="invalid_exact_evm_nonce_already_used",
                error_message="nonce already used",
            )
        seen_nonces.add(nonce)
        return await real_settle(self, payload, requirements)

    monkeypatch.setattr(server.HTTPFacilitatorClient, "settle", _settle_once_per_nonce)

    recovery_secret, recovery_hash = _recovery_secret_pair()
    body = {
        "agent_id": "buyer-replay",
        "authority_ceiling_usd": "9.00",
        "recovery_secret_hash": recovery_hash,
    }
    payment_required = _unpaid_402(client, body)
    header, _payer = _signed_header(payment_required)
    payment_nonce = _nonce_from_header(header)

    first = client.post("/sessions", json=body, headers={"PAYMENT-SIGNATURE": header})
    assert first.status_code == 200, first.text
    session_id = first.json()["session_id"]
    original_token = first.json()["root_capability"]["token"]

    # The buyer (or a naive automatic retry) resends the IDENTICAL signed
    # payload -- the facilitator itself rejects it before this service's
    # own application code (handle_session_request) ever runs again.
    retry = client.post("/sessions", json=body, headers={"PAYMENT-SIGNATURE": header})
    assert retry.status_code == 402, retry.text
    assert "PAID" not in retry.text

    # The first purchase is completely unaffected: same session, same
    # root, same live credential -- no second row, no second charge, no
    # rotation triggered by the rejected retry.
    assert _session_row_count(capability_store) == 1
    assert _live_token_count(capability_store, session_id) == 1
    info = capability_store.authorize(original_token, session_id, "reserve")
    assert info.delegation_id == session_id
    from decimal import Decimal

    root = core_store.get(session_id)
    assert root is not None
    assert root.authority_usd == Decimal("9.00")

    # The buyer can still recover using the payment_nonce/secret they
    # always held -- the rejected replay did not poison recovery.
    recover_resp = client.post(
        "/sessions/recover",
        json={"payment_nonce": payment_nonce, "recovery_secret": recovery_secret},
    )
    assert recover_resp.status_code == 200, recover_resp.text
    assert recover_resp.json()["session_id"] == session_id
