"""Service-wiring tests for hosted/a2a_economic_authority/server.py's
Phase C `/sessions` route.

Skips automatically unless the hosted extra (fastapi/cdp-sdk/x402) is
installed AND CDP + seller-address env vars are set -- exactly the same
pattern `hosted/work_economics/`'s own
`test_work_economics_service.py` already uses, for the same reason: even
an "unpaid request" check needs a real facilitator handshake
(`sync_facilitator_on_start=True`), so there is no way to test this
layer honestly without real credentials. Never performs a real payment.

The actual session-creation business logic (idempotency, conflict
detection, credential issuance) is tested exhaustively and without any
network dependency in `test_a2a_economic_authority_sessions.py`. This
file only proves the HTTP/x402 wiring around it is assembled correctly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HOSTED_DIR = Path(__file__).resolve().parents[3] / "hosted" / "a2a_economic_authority"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

import pytest  # noqa: E402

pytest.importorskip("a2a")
pytest.importorskip("cdp")
pytest.importorskip("x402")

_HAVE_ENV = bool(
    os.environ.get("CDP_API_KEY_ID")
    and os.environ.get("CDP_API_KEY_SECRET")
    and os.environ.get("ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS")
)

pytestmark = pytest.mark.skipif(
    not _HAVE_ENV,
    reason="requires CDP_API_KEY_ID/CDP_API_KEY_SECRET/ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS",
)


@pytest.fixture
def client(tmp_path):
    import server
    from fastapi.testclient import TestClient

    app = server.build_app(
        base_url="http://testserver/",
        db_path=tmp_path / "authority.sqlite3",
        capability_db_path=tmp_path / "capabilities.sqlite3",
    )
    return TestClient(app)


def test_unpaid_sessions_request_returns_402_with_payment_requirements(client):
    import base64
    import json

    resp = client.post(
        "/sessions", json={"agent_id": "buyer", "authority_ceiling_usd": "10.00"}
    )
    assert resp.status_code == 402
    encoded = resp.headers.get("payment-required")
    assert encoded is not None
    payment_required = json.loads(base64.b64decode(encoded))
    accepted = payment_required["accepts"][0]
    assert accepted["network"] == "eip155:84532"
    assert accepted["scheme"] == "exact"
    assert accepted["payTo"] == os.environ["ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS"]
    assert accepted["maxAmountRequired"] is not None


def test_existing_a2a_and_claim_routes_are_unaffected_by_session_wiring(client):
    """Adding the x402 middleware for /sessions must not gate any
    existing Phase B route behind payment -- only POST /sessions."""
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200

    resp = client.post("/capabilities/claim", json={"claim_id": "does-not-exist"})
    assert resp.status_code in (400, 401, 403)
