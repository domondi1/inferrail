"""Contract test for `examples/economic_authority_session.py`.

Imports and calls the EXAMPLE'S OWN functions (not a reimplementation)
against a real server subprocess and real A2A transport -- the same
"never hand-roll what the SDK/example already does" discipline this
repo's other transport tests already follow (see
`test_a2a_economic_authority_transport.py`'s module docstring). This is
what keeps `docs/capabilities/economic-authority.md` and the runnable
example from silently drifting out of sync with the real server: any
change to a response shape, field name, or op payload that would break a
real external agent following the example breaks this test too.

The payment step itself (`buy_session`'s `POST /sessions` exchange) is
not exercised here -- that exact request/response cycle is already
proven against the real x402/CDP verification path in
`test_a2a_economic_authority_payment_settlement_boundary.py` and
`test_a2a_economic_authority_session_service.py`, and this repo's policy
is to never spend another real testnet payment to re-prove already-proven
payment-rail behavior. Instead, a session is seeded directly via
`sessions.create_or_recover_session` (bypassing x402, exactly like
`test_a2a_economic_authority_transport.py`'s own real-A2A-lifecycle test
already does) -- standing in for "the buyer already completed the
payment step in `buy_session`, and now holds `payment_nonce`,
`recovery_secret`, and the root token it returned." Everything from
there on (discovery, recovery, status, reserve, claim, consume, settle,
revoke) runs through the example's real functions against a real running
server.

Skips automatically unless the hosted extra (a2a-sdk) is installed,
exactly like the other real-transport tests in this file's package.
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTED_DIR = REPO_ROOT / "hosted" / "a2a_economic_authority"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

import os  # noqa: E402

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
    reason="requires CDP_API_KEY_ID/CDP_API_KEY_SECRET/ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS "
    "-- /sessions/recover is only wired when the session-purchase route is configured",
)

from _a2a_economic_authority_client import agent_process, free_port  # noqa: E402
from a2a.types import TaskState  # noqa: E402
from capabilities import CapabilityStore  # noqa: E402
from core import EconomicAuthorityStore  # noqa: E402
from sessions import create_or_recover_session  # noqa: E402

_EXAMPLE_PATH = REPO_ROOT / "examples" / "economic_authority_session.py"
_spec = importlib.util.spec_from_file_location("economic_authority_session_example", _EXAMPLE_PATH)
assert _spec is not None and _spec.loader is not None
example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(example)


def _recovery_pair() -> tuple[str, str]:
    import hashlib
    import secrets

    secret = secrets.token_urlsafe(32)
    return secret, hashlib.sha256(secret.encode("utf-8")).hexdigest()


async def test_example_drives_the_full_lifecycle_against_a_real_server(tmp_path):
    db_path = tmp_path / "authority.sqlite3"
    cap_db_path = tmp_path / "capabilities.sqlite3"
    log_path = tmp_path / "server.log"

    core_store = EconomicAuthorityStore(db_path)
    cap_store = CapabilityStore(cap_db_path)
    recovery_secret, recovery_secret_hash = _recovery_pair()
    payment_nonce = "example-contract-test-nonce"
    purchase = create_or_recover_session(
        core_store,
        cap_store,
        payment_nonce=payment_nonce,
        agent_id="example-external-agent",
        authority_ceiling_usd=Decimal("10.00"),
        service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=recovery_secret_hash,
    )
    session_id = purchase.session_id
    assert purchase.plaintext_token is not None

    port = free_port()
    with agent_process(port, db_path, cap_db_path, log_path) as base_url:
        # Step 1: discovery -- the example's own function, against the
        # real server's real Agent Card.
        card = example.discover_agent_card(base_url)
        assert card["name"] == "Inferrail Economic Authority"
        assert {"reserve", "grant", "consume", "settle", "status", "revoke"} <= {
            s["id"] for s in card["skills"]
        }

        # Recovery (illustrative step 7): the example's own function,
        # using only payment_nonce + recovery_secret -- never session_id
        # read out of a purchase response first.
        recovered_session_id, root_token = example.recover_session(
            base_url, payment_nonce, recovery_secret
        )
        assert recovered_session_id == session_id
        assert root_token != purchase.plaintext_token, (
            "recovery must mint a genuinely fresh credential, not echo the original"
        )

        root_client = await example.make_a2a_client(base_url, root_token, "root")
        root_ctx = example.call_context("root")

        # status
        status_task = await example.call_op(
            root_client, root_ctx, {"op": "status", "delegation_id": session_id}
        )
        assert status_task.status.state == TaskState.TASK_STATE_COMPLETED
        status_artifact = example.first_artifact(status_task)
        assert status_artifact["delegation"]["delegation_id"] == session_id
        assert status_artifact["delegation"]["authority_usd"] == "10"

        # reserve
        child_id = f"child-of-{session_id}"
        reserve_task = await example.call_op(
            root_client,
            root_ctx,
            {
                "op": "reserve",
                "event_id": f"evt:{child_id}:reserve",
                "parent_id": session_id,
                "delegation_id": child_id,
                "agent_id": "example-worker",
                "maximum_usd": "1.00",
            },
        )
        assert reserve_task.status.state == TaskState.TASK_STATE_COMPLETED
        claim_id = example.first_artifact(reserve_task)["credential_claim_id"]
        assert claim_id is not None

        # claim (plain HTTP, outside A2A)
        child_token = example.claim_credential(base_url, claim_id, root_token)
        assert child_token and child_token != root_token

        child_client = await example.make_a2a_client(base_url, child_token, "child")
        child_ctx = example.call_context("child")

        # consume
        consume_task = await example.call_op(
            child_client,
            child_ctx,
            {
                "op": "consume",
                "event_id": f"evt:{child_id}:consume",
                "delegation_id": child_id,
                "amount_usd": "0.25",
            },
        )
        assert consume_task.status.state == TaskState.TASK_STATE_COMPLETED
        assert example.first_artifact(consume_task)["delegation"]["consumed_usd"] == "0.25"

        # settle
        settle_task = await example.call_op(
            child_client,
            child_ctx,
            {
                "op": "settle",
                "event_id": f"evt:{child_id}:settle",
                "delegation_id": child_id,
                "outcome": "SUCCESS",
            },
        )
        assert settle_task.status.state == TaskState.TASK_STATE_COMPLETED

        # revoke
        revoke_task = await example.call_op(
            root_client,
            root_ctx,
            {"op": "revoke", "event_id": f"evt:{session_id}:revoke", "delegation_id": session_id},
        )
        assert revoke_task.status.state == TaskState.TASK_STATE_COMPLETED
        revoke_artifact = example.first_artifact(revoke_task)
        assert session_id in revoke_artifact["revoked_delegation_ids"]

    root_state = core_store.get(session_id)
    assert root_state is not None
    assert root_state.consumed_usd == Decimal("0.25")
    assert root_state.active is False
