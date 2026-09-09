"""Validates the machine-readable schemas published under
`docs/capabilities/schemas/economic-authority/` against REAL request/
response bodies produced by the actual code -- not hand-written examples
that could silently drift from what the server actually does. This is
the automated guard `docs/capabilities/economic-authority.md` and its
schemas rely on to never go stale.

Skips automatically unless `jsonschema` is importable (a common
transitive dependency, not a declared direct one -- this test degrades
to skipped, never failing, on an environment that lacks it). The
request/response/error/recovery schemas need no hosted extra at all
(`sessions.py` is transport-independent); the receipt schema additionally
needs the `a2a` extra, gated separately below.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTED_DIR = REPO_ROOT / "hosted" / "a2a_economic_authority"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))
SCHEMA_DIR = REPO_ROOT / "docs" / "capabilities" / "schemas" / "economic-authority"

import pytest  # noqa: E402

jsonschema = pytest.importorskip("jsonschema")

from capabilities import CapabilityStore  # noqa: E402
from core import EconomicAuthorityStore  # noqa: E402
from sessions import handle_session_recovery_request, handle_session_request  # noqa: E402


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


def _recovery_pair() -> tuple[str, str]:
    secret = secrets.token_urlsafe(32)
    return secret, hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _body(secret_hash: str, ceiling: str = "10.00") -> dict:
    return {
        "agent_id": "buyer",
        "authority_ceiling_usd": ceiling,
        "recovery_secret_hash": secret_hash,
    }


@pytest.fixture
def core(tmp_path) -> EconomicAuthorityStore:
    return EconomicAuthorityStore(tmp_path / "authority.sqlite3")


@pytest.fixture
def capabilities(tmp_path) -> CapabilityStore:
    return CapabilityStore(tmp_path / "capabilities.sqlite3")


def test_a_valid_purchase_request_matches_request_schema():
    _secret, secret_hash = _recovery_pair()
    body = {
        "agent_id": "buyer",
        "authority_ceiling_usd": "10.00",
        "recovery_secret_hash": secret_hash,
    }
    jsonschema.validate(body, _schema("request.schema.json"))


def test_a_real_200_purchase_response_matches_response_schema(core, capabilities):
    _secret, secret_hash = _recovery_pair()
    status, body = handle_session_request(
        core, capabilities,
        body=_body(secret_hash),
        payment_nonce="schema-test-nonce", service_fee_usd=Decimal("0.05"),
    )
    assert status == 200
    jsonschema.validate(body, _schema("response.schema.json"))


def test_a_real_duplicate_purchase_response_matches_response_schema(core, capabilities):
    """The `root_capability: null` / `note` shape -- a distinct branch of
    the same schema."""
    _secret, secret_hash = _recovery_pair()
    handle_session_request(
        core, capabilities,
        body=_body(secret_hash),
        payment_nonce="schema-test-dup-nonce", service_fee_usd=Decimal("0.05"),
    )
    status, body = handle_session_request(
        core, capabilities,
        body=_body(secret_hash),
        payment_nonce="schema-test-dup-nonce", service_fee_usd=Decimal("0.05"),
    )
    assert status == 200
    assert body["root_capability"] is None
    jsonschema.validate(body, _schema("response.schema.json"))


def test_real_error_responses_match_error_schema(core, capabilities):
    schema = _schema("error.schema.json")

    status, body = handle_session_request(
        core, capabilities, body={"authority_ceiling_usd": "10.00"},
        payment_nonce="n", service_fee_usd=Decimal("0.05"),
    )
    assert status == 400
    jsonschema.validate(body, schema)

    _secret, secret_hash = _recovery_pair()
    handle_session_request(
        core, capabilities,
        body=_body(secret_hash),
        payment_nonce="n-conflict", service_fee_usd=Decimal("0.05"),
    )
    status, body = handle_session_request(
        core, capabilities,
        body=_body(secret_hash, ceiling="999.00"),
        payment_nonce="n-conflict", service_fee_usd=Decimal("0.05"),
    )
    assert status == 409
    jsonschema.validate(body, schema)

    status, body = handle_session_recovery_request(
        core, capabilities, body={"payment_nonce": "never-purchased", "recovery_secret": "x"},
    )
    assert status == 403
    jsonschema.validate(body, schema)


def test_real_recovery_request_and_response_match_recovery_schema(core, capabilities):
    schema = _schema("recovery.schema.json")
    secret, secret_hash = _recovery_pair()
    handle_session_request(
        core, capabilities,
        body=_body(secret_hash),
        payment_nonce="schema-test-recovery-nonce", service_fee_usd=Decimal("0.05"),
    )
    request_body = {"payment_nonce": "schema-test-recovery-nonce", "recovery_secret": secret}
    jsonschema.validate(request_body, schema)

    status, response_body = handle_session_recovery_request(core, capabilities, body=request_body)
    assert status == 200
    jsonschema.validate(response_body, schema)


# -- receipt schema: needs a real A2A executor round trip ------------------

pytest.importorskip("a2a")

import asyncio  # noqa: E402

from _a2a_economic_authority_client import agent_process, free_port, make_client  # noqa: E402
from a2a.helpers import get_data_parts, new_data_message  # noqa: E402
from a2a.types import Role, SendMessageRequest, TaskState  # noqa: E402
from sessions import create_or_recover_session  # noqa: E402


def test_real_a2a_receipt_artifacts_match_receipt_schema(tmp_path):
    schema = _schema("receipt.schema.json")
    db_path = tmp_path / "authority.sqlite3"
    cap_db_path = tmp_path / "capabilities.sqlite3"
    core = EconomicAuthorityStore(db_path)
    capabilities = CapabilityStore(cap_db_path)
    _secret, secret_hash = _recovery_pair()
    purchase = create_or_recover_session(
        core, capabilities, payment_nonce="schema-receipt-nonce", agent_id="buyer",
        authority_ceiling_usd=Decimal("5.00"), service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=secret_hash,
    )
    session_id = purchase.session_id
    assert purchase.plaintext_token is not None

    async def _run() -> list[dict]:
        receipts = []
        port = free_port()
        with agent_process(port, db_path, cap_db_path, tmp_path / "server.log") as base_url:
            client = await make_client(base_url, purchase.plaintext_token)
            ctx_state = {"sessionId": "sess"}

            async def send(payload: dict) -> dict:
                from a2a.client.client import ClientCallContext

                message = new_data_message(payload, role=Role.ROLE_USER)
                request = SendMessageRequest(message=message)
                async for response in client.send_message(
                    request, context=ClientCallContext(state=ctx_state)
                ):
                    if response.HasField("task"):
                        task = response.task
                        assert task.status.state == TaskState.TASK_STATE_COMPLETED
                        for artifact in task.artifacts:
                            for item in get_data_parts(artifact.parts):
                                if isinstance(item, dict) and item.get("note"):
                                    return item
                raise AssertionError("no receipt artifact returned")

            receipts.append(await send({"op": "status", "delegation_id": session_id}))
            receipts.append(
                await send(
                    {
                        "op": "grant",
                        "delegation_id": session_id,
                        "event_id": "evt:grant",
                        "amount_usd": "1.00",
                    }
                )
            )
            receipts.append(
                await send(
                    {
                        "op": "consume",
                        "delegation_id": session_id,
                        "event_id": "evt:consume",
                        "amount_usd": "0.50",
                    }
                )
            )
            receipts.append(
                await send(
                    {
                        "op": "settle",
                        "delegation_id": session_id,
                        "event_id": "evt:settle",
                        "outcome": "SUCCESS",
                    }
                )
            )
        return receipts

    receipts = asyncio.run(_run())
    assert len(receipts) == 4
    for receipt in receipts:
        jsonschema.validate(receipt, schema)
