"""Paid-path tests for hosted/work_economics/service.py.

Drives the real service (`create_app`) through the real x402 2.22.0
middleware with real, offline-signed EIP-3009 payments. Only the
facilitator is replaced (`_x402_fake_facilitator.py`), so these run with
no CDP credentials and no network access.

The fake is wired in from the test side -- by replacing the two names
`service.py` uses to build its CDP facilitator client
(`create_facilitator_config`, `HTTPFacilitatorClient`) on the freshly
loaded module -- so `hosted/work_economics/` itself is not modified at
all. Everything `create_app` builds on top of that client (the
`x402ResourceServer`, the EVM exact scheme registration, the route config
and payment middleware) is the real, unmodified production wiring.

Two kinds of test live here:

1. **Characterization tests** pin the service's CURRENT paid-path
   behavior, including behavior that is known to be wrong. They exist so
   that the upcoming payment-correctness changes are made against a
   measured baseline, and so that any behavior change is visible in the
   diff of this file rather than silent.

2. **Known-defect tests** (`xfail(strict=True)`) assert the behavior the
   service SHOULD have. Each currently fails for the documented reason.
   `strict=True` means a fix that makes one pass will fail the suite until
   the marker is removed deliberately, alongside the fix.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytest.importorskip("cdp")
pytest.importorskip("x402")
pytest.importorskip("eth_account")
pytest.importorskip("fastapi")

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
HOSTED_DIR = REPO_ROOT / "hosted" / "work_economics"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _x402_fake_facilitator import (  # noqa: E402
    FakeFacilitator,
    decode_payment_required,
    settle_failure,
    sign_payment,
)
from eth_account import Account  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PAY_TO = "0x000000000000000000000000000000000000dEaD"

VALID_BODY: dict[str, Any] = {
    "work_id": "work-1",
    "events": [
        {
            "resource_class": "inference",
            "supplier": "example-supplier",
            "known_cost_usd": "0.02",
            "price_basis": "LIST_PRICE_PUBLISHED",
            "currency": "USD",
            "status": "success",
        }
    ],
    "outcome_status": "success",
}

OTHER_BODY: dict[str, Any] = {
    **VALID_BODY,
    "work_id": "work-2",
    "events": [{**VALID_BODY["events"][0], "known_cost_usd": "9.99"}],
}

INVALID_BODY: dict[str, Any] = {
    "work_id": "work-bad",
    "events": [{**VALID_BODY["events"][0], "price_basis": "NOT_A_BASIS"}],
}


def _load(name: str, filename: str) -> ModuleType:
    """Loads a hosted/work_economics module by explicit path (see
    test_cost_gateway_service.py for why: several hosted services ship a
    module literally named `service`)."""
    spec = importlib.util.spec_from_file_location(name, HOSTED_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    # service.py reads the pay-to address at import time; set it before
    # loading, exactly as a deployment's environment would.
    monkeypatch.setenv("X402_SELLER_PAY_TO_ADDRESS", PAY_TO)
    # create_app reads these unconditionally; with the facilitator
    # construction replaced below they are never sent anywhere.
    monkeypatch.setenv("CDP_API_KEY_ID", "unused-in-tests")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "unused-in-tests")
    _load("capability", "capability.py")
    _load("store", "store.py")
    return _load("work_economics_service", "service.py")


def _create_app_with(service: ModuleType, db_path: Path, facilitator: FakeFacilitator) -> Any:
    """Builds the real app with `facilitator` in place of the CDP client.

    `service` is a fresh, test-private module object (see `_load`), so
    rebinding its two facilitator-construction names affects nothing else.
    """
    service.create_facilitator_config = lambda **_kwargs: None
    service.HTTPFacilitatorClient = lambda _config: facilitator
    return service.create_app(db_path)


class Seller:
    def __init__(self, service: ModuleType, db_path: Path, facilitator: FakeFacilitator):
        self.db_path = db_path
        self.facilitator = facilitator
        self.client = TestClient(_create_app_with(service, db_path, facilitator))

    def unpaid(self, purchase_id: str | None, body: dict[str, Any]) -> Any:
        headers = {"X-Purchase-Id": purchase_id} if purchase_id else {}
        return self.client.post("/invoke", json=body, headers=headers)

    def payment_for(self, body: dict[str, Any], account: Any = None) -> Any:
        resp = self.unpaid("probe-for-requirements", body)
        assert resp.status_code == 402
        return sign_payment(decode_payment_required(resp), account)

    def paid(
        self,
        purchase_id: str | None,
        body: dict[str, Any],
        *,
        account: Any = None,
        signed: Any = None,
    ) -> tuple[Any, Any]:
        signed = signed if signed is not None else self.payment_for(body, account)
        headers = {"PAYMENT-SIGNATURE": signed.header}
        if purchase_id is not None:
            headers["X-Purchase-Id"] = purchase_id
        return self.client.post("/invoke", json=body, headers=headers), signed

    def row(self, purchase_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            found = conn.execute(
                "SELECT * FROM purchases WHERE purchase_id = ?", (purchase_id,)
            ).fetchone()
            return dict(found) if found is not None else None

    def row_count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            (count,) = conn.execute("SELECT COUNT(*) FROM purchases").fetchone()
            return int(count)

    def settle_count(self) -> int:
        return len(self.facilitator.calls_to("settle"))


@pytest.fixture
def seller(service: ModuleType, tmp_path: Path) -> Seller:
    return Seller(service, tmp_path / "purchases.sqlite3", FakeFacilitator())


def _seller_with(service: ModuleType, tmp_path: Path, facilitator: FakeFacilitator) -> Seller:
    return Seller(service, tmp_path / "purchases.sqlite3", facilitator)


# ============================================================================
# 1. Characterization: current behavior, pinned as-is
# ============================================================================


def test_health_and_manifest_need_no_credentials_or_network(seller: Seller):
    assert seller.client.get("/health").json() == {"status": "ok"}
    manifest = seller.client.get("/manifest").json()
    assert manifest["price"]["pay_to"] == PAY_TO
    assert manifest["price"]["network_class"] == "TESTNET"
    assert seller.facilitator.calls == []


def test_unpaid_invoke_returns_402_and_writes_nothing(seller: Seller):
    resp = seller.unpaid("p-unpaid", VALID_BODY)
    assert resp.status_code == 402
    accepted = decode_payment_required(resp).accepts[0]
    assert accepted.pay_to == PAY_TO
    assert accepted.network == "eip155:84532"
    assert accepted.amount == "50000"  # $0.05 in 6-decimal test USDC
    assert "paymentFlow" not in (accepted.extra or {}), "default (settle-after) flow today"
    assert seller.row_count() == 0


def test_first_paid_purchase_verifies_then_executes_then_settles(seller: Seller):
    snapshots: list[dict[str, Any] | None] = []
    seller.facilitator.on_settle = lambda _p: snapshots.append(seller.row("p-1"))

    resp, signed = seller.paid("p-1", VALID_BODY)

    assert resp.status_code == 200
    body = resp.json()
    assert body["newly_charged"] is True
    assert body["newly_executed"] is True
    assert body["commercial_receipt"]["status"] == "DELIVERED"
    assert body["result"]["known_total_cost_usd"] == "0.02"
    assert [c.method for c in seller.facilitator.calls if c.nonce == signed.nonce] == [
        "verify",
        "settle",
    ]
    # CURRENT: by the time settlement starts, the row already records a
    # delivered execution (see the E1-1 defect test below).
    assert snapshots and snapshots[0] is not None
    assert snapshots[0]["status"] == "EXECUTED"
    assert json.loads(snapshots[0]["receipt_json"])["status"] == "DELIVERED"
    assert "payment-response" in {k.lower() for k in resp.headers}


def test_settlement_failure_returns_402_but_the_row_records_delivery(
    service: ModuleType, tmp_path: Path
):
    seller = _seller_with(service, tmp_path, FakeFacilitator(settle_outcomes=[settle_failure()]))
    resp, _ = seller.paid("p-fail", VALID_BODY)
    assert resp.status_code == 402
    row = seller.row("p-fail")
    assert row is not None
    assert row["status"] == "EXECUTED"
    assert json.loads(row["receipt_json"])["status"] == "DELIVERED"


def test_settlement_exception_returns_402_but_the_row_records_delivery(
    service: ModuleType, tmp_path: Path
):
    seller = _seller_with(
        service, tmp_path, FakeFacilitator(settle_outcomes=[TimeoutError("read timed out")])
    )
    resp, _ = seller.paid("p-timeout", VALID_BODY)
    assert resp.status_code == 402
    row = seller.row("p-timeout")
    assert row is not None and row["status"] == "EXECUTED"


def test_retrying_the_identical_signed_request_is_rejected_by_verification(seller: Seller):
    """Depends on the fake's model of EIP-3009 single-use nonces: once the
    nonce has settled, facilitator /verify rejects it. The buyer therefore
    cannot retrieve their already-paid result with the same payment."""
    first, signed = seller.paid("p-retry", VALID_BODY)
    assert first.status_code == 200
    second, _ = seller.paid("p-retry", VALID_BODY, signed=signed)
    assert second.status_code == 402
    assert seller.settle_count() == 1


def test_fresh_payment_on_a_completed_purchase_is_settled_again(seller: Seller):
    account = Account.create()
    first, _ = seller.paid("p-twice", VALID_BODY, account=account)
    second, _ = seller.paid("p-twice", VALID_BODY, account=account)
    assert first.status_code == second.status_code == 200
    assert second.json()["newly_charged"] is False
    assert second.json()["newly_executed"] is False
    assert second.json()["result"] == first.json()["result"]
    # CURRENT: the second authorization is settled too -- a second charge
    # for one purchase_id while the response says newly_charged=false.
    assert seller.settle_count() == 2


def test_same_purchase_id_with_a_different_body_returns_the_first_result(seller: Seller):
    first, _ = seller.paid("p-body", VALID_BODY)
    second, _ = seller.paid("p-body", OTHER_BODY)
    assert second.status_code == 200
    assert second.json()["result"] == first.json()["result"]
    assert second.json()["result"]["work_id"] == "work-1"


def test_same_purchase_id_from_a_different_payer_returns_the_first_result(seller: Seller):
    first, _ = seller.paid("p-payer", VALID_BODY, account=Account.create())
    second, _ = seller.paid("p-payer", VALID_BODY, account=Account.create())
    assert second.status_code == 200
    assert second.json()["result"] == first.json()["result"]
    assert seller.settle_count() == 2


def test_invalid_events_after_payment_return_422_and_are_not_settled(seller: Seller):
    resp, _ = seller.paid("p-invalid", INVALID_BODY)
    assert resp.status_code == 422
    assert resp.json()["commercial_receipt"]["status"] == "INPUT_REJECTED"
    assert resp.json()["newly_charged"] is True
    assert seller.settle_count() == 0, "x402 never settles a >=400 response"
    row = seller.row("p-invalid")
    assert row is not None and row["status"] == "EXECUTED"


def test_unpaid_invalid_request_gets_402_not_422(seller: Seller):
    assert seller.unpaid("p-invalid-unpaid", INVALID_BODY).status_code == 402


def test_a_corrected_retry_after_422_returns_the_cached_422(seller: Seller):
    seller.paid("p-422", INVALID_BODY)
    retry, _ = seller.paid("p-422", VALID_BODY)
    assert retry.status_code == 422
    assert retry.json()["commercial_receipt"]["status"] == "INPUT_REJECTED"


def test_missing_purchase_id_after_payment_returns_400_and_is_not_settled(seller: Seller):
    resp, _ = seller.paid(None, VALID_BODY)
    assert resp.status_code == 400
    assert seller.settle_count() == 0


def test_payment_proof_ref_stores_the_whole_signed_payload(seller: Seller):
    _, signed = seller.paid("p-ref", VALID_BODY)
    row = seller.row("p-ref")
    assert row is not None
    ref = row["payment_proof_ref"]
    assert ref != signed.nonce
    assert signed.nonce in ref
    assert "signature" in ref


def test_no_settlement_transaction_is_persisted(seller: Seller):
    resp, _ = seller.paid("p-tx", VALID_BODY)
    tx_hash = json.loads(
        __import__("base64").b64decode(resp.headers["payment-response"])
    )["transaction"]
    assert tx_hash.startswith("0x")
    with sqlite3.connect(seller.db_path) as conn:
        dump = "\n".join(conn.iterdump())
    assert tx_hash not in dump


# ============================================================================
# 2. Known defects: desired behavior, currently failing (strict xfail)
# ============================================================================


@pytest.mark.xfail(
    strict=True,
    reason="E1-1: delivery is recorded before settlement, and kept when settlement fails",
)
def test_e1_1_settlement_failure_never_leaves_a_delivered_record(
    service: ModuleType, tmp_path: Path
):
    seller = _seller_with(service, tmp_path, FakeFacilitator(settle_outcomes=[settle_failure()]))
    resp, _ = seller.paid("p-e1-1", VALID_BODY)
    assert resp.status_code == 402
    row = seller.row("p-e1-1")
    assert row is None or row["status"] != "EXECUTED"


@pytest.mark.xfail(
    strict=True,
    reason="E1-1: the purchase is marked delivered before settlement has started",
)
def test_e1_1_no_delivery_is_recorded_before_settlement_starts(seller: Seller):
    snapshots: list[dict[str, Any] | None] = []
    seller.facilitator.on_settle = lambda _p: snapshots.append(seller.row("p-e1-1b"))
    seller.paid("p-e1-1b", VALID_BODY)
    assert snapshots
    assert snapshots[0] is None or snapshots[0]["receipt_json"] is None


@pytest.mark.xfail(
    strict=True,
    reason="E1-2: a fresh payment on a completed purchase is settled a second time",
)
def test_e1_2_fresh_payment_on_a_completed_purchase_is_never_settled(seller: Seller):
    account = Account.create()
    seller.paid("p-e1-2", VALID_BODY, account=account)
    seller.paid("p-e1-2", VALID_BODY, account=account)
    assert seller.settle_count() == 1


@pytest.mark.xfail(
    strict=True,
    reason="E1-3: purchase_id is not bound to the request body",
)
def test_e1_3_same_purchase_id_with_a_different_body_is_a_409_before_payment(seller: Seller):
    seller.paid("p-e1-3a", VALID_BODY)
    second, _ = seller.paid("p-e1-3a", OTHER_BODY)
    assert second.status_code == 409
    assert seller.settle_count() == 1


@pytest.mark.xfail(
    strict=True,
    reason="E1-3: purchase_id is not bound to the payer",
)
def test_e1_3_same_purchase_id_from_a_different_payer_is_a_409_before_payment(seller: Seller):
    seller.paid("p-e1-3b", VALID_BODY, account=Account.create())
    second, _ = seller.paid("p-e1-3b", VALID_BODY, account=Account.create())
    assert second.status_code == 409
    assert seller.settle_count() == 1


@pytest.mark.xfail(
    strict=True,
    reason="E1-4: the settlement transaction hash is never persisted",
)
def test_e1_4_settlement_transaction_is_persisted(seller: Seller):
    resp, _ = seller.paid("p-e1-4", VALID_BODY)
    tx_hash = json.loads(
        __import__("base64").b64decode(resp.headers["payment-response"])
    )["transaction"]
    with sqlite3.connect(seller.db_path) as conn:
        dump = "\n".join(conn.iterdump())
    assert tx_hash in dump


def test_e1_5_docs_and_behavior_agree_about_settling_rejected_invocations(seller: Seller):
    doc = " ".join((REPO_ROOT / "docs" / "capabilities" / "work-economics.md").read_text().split())
    docs_say_not_settled = "the payment is verified but not settled" in doc
    assert docs_say_not_settled, "precondition: the docs sentence this test checks still exists"
    seller.paid("p-e1-5", INVALID_BODY)
    assert seller.settle_count() == 0


@pytest.mark.xfail(
    strict=True,
    reason="E1-5: invalid requests are only rejected after a payment is requested",
)
def test_e1_5_invalid_request_is_rejected_before_any_payment_is_requested(seller: Seller):
    assert seller.unpaid("p-e1-5b", INVALID_BODY).status_code == 422


@pytest.mark.xfail(
    strict=True,
    reason="E1-8: nonce extraction uses attribute access on a dict and falls back to "
    "storing the whole signed payload",
)
def test_e1_8_payment_proof_ref_is_the_authorization_nonce(seller: Seller):
    _, signed = seller.paid("p-e1-8", VALID_BODY)
    row = seller.row("p-e1-8")
    assert row is not None
    assert row["payment_proof_ref"] == signed.nonce
