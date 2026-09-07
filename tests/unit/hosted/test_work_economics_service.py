"""Service-wiring tests for hosted/work_economics/service.py.

Skips automatically unless the hosted extra (fastapi/cdp-sdk/x402) is
installed AND CDP + seller-address env vars are set -- the same pattern
this repo already uses for the OpenAI integration test that needs a real
API key. Never performs a real payment: only checks that /health,
/manifest, and the unpaid-402 path are wired correctly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HOSTED_DIR = Path(__file__).resolve().parents[3] / "hosted" / "work_economics"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

import pytest  # noqa: E402

pytest.importorskip("cdp")
pytest.importorskip("x402")

_HAVE_ENV = bool(
    os.environ.get("CDP_API_KEY_ID")
    and os.environ.get("CDP_API_KEY_SECRET")
    and os.environ.get("X402_SELLER_PAY_TO_ADDRESS")
)

pytestmark = pytest.mark.skipif(
    not _HAVE_ENV,
    reason="requires CDP_API_KEY_ID/CDP_API_KEY_SECRET/X402_SELLER_PAY_TO_ADDRESS",
)


@pytest.fixture
def client(tmp_path):
    import service
    from fastapi.testclient import TestClient

    app = service.create_app(tmp_path / "test_purchases.sqlite3")
    return TestClient(app)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_manifest_is_unauthenticated_and_describes_the_capability(client):
    resp = client.get("/manifest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["capability"] == "inferrail-work-economics"
    assert body["capability_version"] == "work-economics-v1"
    assert body["price"]["network_class"] == "TESTNET"
    assert "invoke_endpoint" in body


def test_unpaid_invoke_returns_402_with_payment_requirements(client):
    import base64
    import json

    resp = client.post(
        "/invoke",
        headers={"X-Purchase-Id": "test-service-wiring-001"},
        json={
            "work_id": "w1",
            "events": [
                {
                    "resource_class": "inference",
                    "supplier": "s",
                    "known_cost_usd": "0.02",
                    "price_basis": "LIST_PRICE_PUBLISHED",
                    "currency": "USD",
                    "status": "success",
                }
            ],
        },
    )
    assert resp.status_code == 402
    # This SDK version encodes PaymentRequirements as base64 JSON in the
    # Payment-Required response header (the buyer's own x402 client reads
    # it from there, with the body as a fallback -- see
    # examples/work_economics_purchase.py's use of
    # http_client.get_payment_required_response).
    encoded = resp.headers.get("payment-required")
    assert encoded is not None
    payment_required = json.loads(base64.b64decode(encoded))
    accepted = payment_required["accepts"][0]
    assert accepted["network"] == "eip155:84532"
    assert accepted["scheme"] == "exact"
    assert accepted["payTo"] == os.environ["X402_SELLER_PAY_TO_ADDRESS"]


def test_invoke_without_purchase_id_header_is_rejected_before_payment_check(client):
    resp = client.post("/invoke", json={"work_id": "w1", "events": []})
    assert resp.status_code in (400, 402)
