"""Tests for hosted/a2a_economic_authority/sessions.py's paid-session
creation logic (Phase C). No network, no x402/FastAPI/facilitator
dependency -- like core.py and capabilities.py, this module is
transport-independent and so are these tests: every test here calls
`create_or_recover_session`/`handle_session_request` directly with a
synthetic `payment_nonce`, exactly as `server.py`'s route calls it AFTER
the x402 middleware has already verified a real payment. The x402/HTTP
wiring itself is covered separately in
`test_a2a_economic_authority_session_service.py`.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTED_DIR = REPO_ROOT / "hosted" / "a2a_economic_authority"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

import pytest  # noqa: E402
from capabilities import CapabilityStore, SessionAuthorizationConflict  # noqa: E402
from core import EconomicAuthorityStore  # noqa: E402
from sessions import create_or_recover_session, handle_session_request  # noqa: E402


@pytest.fixture
def core(tmp_path) -> EconomicAuthorityStore:
    return EconomicAuthorityStore(tmp_path / "authority.sqlite3")


@pytest.fixture
def capabilities(tmp_path) -> CapabilityStore:
    return CapabilityStore(tmp_path / "capabilities.sqlite3")


def _live_token_ids(capabilities: CapabilityStore, session_id: str) -> list[str]:
    with sqlite3.connect(capabilities.db_path) as conn:
        rows = conn.execute(
            "SELECT token_id FROM capability_tokens WHERE delegation_id = ? AND revoked = 0",
            (session_id,),
        ).fetchall()
        return [row[0] for row in rows]


# -- one valid payment creates exactly one session -------------------------


def test_a_new_payment_nonce_creates_one_root_session_with_a_working_credential(core, capabilities):
    result = create_or_recover_session(
        core,
        capabilities,
        payment_nonce="nonce-1",
        agent_id="buyer-1",
        authority_ceiling_usd=Decimal("10.00"),
        service_fee_usd=Decimal("0.05"),
    )
    assert result.plaintext_token is not None
    assert result.newly_claimed is True
    assert result.authority_ceiling_usd == Decimal("10.00")
    assert result.service_fee_usd == Decimal("0.05")

    root = core.get(result.session_id)
    assert root is not None
    assert root.parent_delegation_id is None
    assert root.authority_usd == Decimal("10.00")
    assert root.agent_id == "buyer-1"

    info = capabilities.authorize(result.plaintext_token, result.session_id, "reserve")
    assert info.delegation_id == result.session_id
    assert "revoke" in info.scopes  # root gets full scopes


def test_service_fee_and_authority_ceiling_are_kept_separate(core, capabilities):
    """The two numbers must never collapse into one figure: the fee paid
    to Inferrail and the buyer-declared coordination ceiling are wildly
    different magnitudes here specifically to catch any code path that
    accidentally conflates them."""
    result = create_or_recover_session(
        core,
        capabilities,
        payment_nonce="nonce-separate",
        agent_id="buyer",
        authority_ceiling_usd=Decimal("500.00"),
        service_fee_usd=Decimal("0.05"),
    )
    assert result.authority_ceiling_usd == Decimal("500.00")
    assert result.service_fee_usd == Decimal("0.05")
    root = core.get(result.session_id)
    assert root is not None
    assert root.authority_usd == Decimal("500.00"), (
        "the root's real authority must be the ceiling, never the service fee"
    )


# -- ordinary duplicate delivery is idempotent ------------------------------


def test_replaying_the_same_payment_nonce_does_not_recharge_or_recreate(core, capabilities):
    first = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-dup", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
    )
    second = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-dup", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
    )
    assert second.session_id == first.session_id
    assert second.plaintext_token is None, (
        "an already-claimed session must never re-expose plaintext"
    )
    assert second.newly_claimed is False

    root = core.get(first.session_id)
    assert root is not None
    assert root.authority_usd == Decimal("10.00"), "authority must never increase on replay"
    assert len(_live_token_ids(capabilities, first.session_id)) == 1


def test_ten_replays_still_leave_exactly_one_live_credential_and_one_root(core, capabilities):
    results = [
        create_or_recover_session(
            core, capabilities, payment_nonce="nonce-many", agent_id="buyer",
            authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
        )
        for _ in range(10)
    ]
    session_ids = {r.session_id for r in results}
    assert len(session_ids) == 1
    claimed = [r for r in results if r.plaintext_token is not None]
    assert len(claimed) == 1, "only the first-ever call may expose plaintext"
    assert len(_live_token_ids(capabilities, results[0].session_id)) == 1


# -- concurrency: a genuine race for the same brand-new nonce ---------------


def test_concurrent_first_calls_for_the_same_new_nonce_mint_exactly_one_credential(
    core, capabilities
):
    """Simulates two requests racing to process the SAME never-before-seen
    payment nonce (e.g. a network-level retry arriving before the first
    request's response is delivered) -- at most one may walk away with a
    working plaintext credential, and both must agree on the same
    session_id."""
    results: list = []
    errors: list = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        try:
            results.append(
                create_or_recover_session(
                    core, capabilities, payment_nonce="nonce-race", agent_id="buyer",
                    authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
                )
            )
        except Exception as exc:  # pragma: no cover - failure path only
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == 2
    assert results[0].session_id == results[1].session_id
    claimed = [r for r in results if r.plaintext_token is not None]
    assert len(claimed) == 1, "exactly one racer must receive the plaintext credential"

    root = core.get(results[0].session_id)
    assert root is not None
    assert root.authority_usd == Decimal("10.00"), "the race must never double-reserve authority"
    assert len(_live_token_ids(capabilities, results[0].session_id)) == 1


# -- payment-proof replay against another session is rejected --------------


def test_reusing_a_nonce_with_a_different_authority_ceiling_is_rejected(core, capabilities):
    create_or_recover_session(
        core, capabilities, payment_nonce="nonce-conflict", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
    )
    with pytest.raises(SessionAuthorizationConflict):
        create_or_recover_session(
            core, capabilities, payment_nonce="nonce-conflict", agent_id="buyer",
            authority_ceiling_usd=Decimal("999.00"), service_fee_usd=Decimal("0.05"),
        )
    # The original session's authority must be completely unaffected.
    purchase = capabilities.get_session_purchase("nonce-conflict")
    assert purchase is not None
    root = core.get(purchase.session_id)
    assert root is not None
    assert root.authority_usd == Decimal("10.00")


def test_reusing_a_nonce_with_a_different_agent_id_is_rejected(core, capabilities):
    create_or_recover_session(
        core, capabilities, payment_nonce="nonce-conflict-agent", agent_id="buyer-a",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
    )
    with pytest.raises(SessionAuthorizationConflict):
        create_or_recover_session(
            core, capabilities, payment_nonce="nonce-conflict-agent", agent_id="buyer-b",
            authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
        )


def test_a_different_nonce_always_creates_a_genuinely_different_session(core, capabilities):
    r1 = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-a", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
    )
    r2 = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-b", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
    )
    assert r1.session_id != r2.session_id
    assert r1.plaintext_token != r2.plaintext_token


# -- restart durability ------------------------------------------------


def test_a_fresh_store_instance_against_the_same_files_sees_the_same_session(tmp_path):
    db_path = tmp_path / "authority.sqlite3"
    cap_path = tmp_path / "capabilities.sqlite3"
    core1 = EconomicAuthorityStore(db_path)
    caps1 = CapabilityStore(cap_path)
    first = create_or_recover_session(
        core1, caps1, payment_nonce="nonce-restart", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
    )

    # Simulates a process restart: brand new store instances, same files.
    core2 = EconomicAuthorityStore(db_path)
    caps2 = CapabilityStore(cap_path)
    second = create_or_recover_session(
        core2, caps2, payment_nonce="nonce-restart", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
    )
    assert second.session_id == first.session_id
    assert second.plaintext_token is None, "restart must not duplicate the credential"
    root = core2.get(first.session_id)
    assert root is not None
    assert root.authority_usd == Decimal("10.00"), "restart must not duplicate or lose authority"


# -- validation ----------------------------------------------------------


def test_non_positive_authority_ceiling_is_rejected(core, capabilities):
    with pytest.raises(ValueError):
        create_or_recover_session(
            core, capabilities, payment_nonce="nonce-neg", agent_id="buyer",
            authority_ceiling_usd=Decimal("0"), service_fee_usd=Decimal("0.05"),
        )
    with pytest.raises(ValueError):
        create_or_recover_session(
            core, capabilities, payment_nonce="nonce-neg2", agent_id="buyer",
            authority_ceiling_usd=Decimal("-5.00"), service_fee_usd=Decimal("0.05"),
        )


def test_non_finite_authority_ceiling_is_rejected(core, capabilities):
    with pytest.raises(ValueError):
        create_or_recover_session(
            core, capabilities, payment_nonce="nonce-nan", agent_id="buyer",
            authority_ceiling_usd=Decimal("NaN"), service_fee_usd=Decimal("0.05"),
        )


def test_empty_agent_id_is_rejected(core, capabilities):
    with pytest.raises(ValueError):
        create_or_recover_session(
            core, capabilities, payment_nonce="nonce-empty-agent", agent_id="",
            authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
        )


def test_equivalent_decimal_strings_do_not_false_conflict(core, capabilities):
    """"10" and "10.00" must be recognized as the same amount on retry --
    matching core.py's own canonical-decimal-comparison discipline."""
    create_or_recover_session(
        core, capabilities, payment_nonce="nonce-canon", agent_id="buyer",
        authority_ceiling_usd=Decimal("10"), service_fee_usd=Decimal("0.05"),
    )
    result = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-canon", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.050"),
    )
    assert result.plaintext_token is None  # recognized as the same retry, not a conflict


# -- handle_session_request: the transport-adjacent request handler --------


def test_handle_session_request_rejects_a_non_object_body(core, capabilities):
    status, body = handle_session_request(
        core, capabilities, body=["not", "an", "object"], payment_nonce="n1",
        service_fee_usd=Decimal("0.05"),
    )
    assert status == 400


def test_handle_session_request_requires_agent_id(core, capabilities):
    status, body = handle_session_request(
        core, capabilities, body={"authority_ceiling_usd": "10.00"}, payment_nonce="n1",
        service_fee_usd=Decimal("0.05"),
    )
    assert status == 400
    assert "agent_id" in body["error"]


def test_handle_session_request_requires_authority_ceiling_usd(core, capabilities):
    status, body = handle_session_request(
        core, capabilities, body={"agent_id": "buyer"}, payment_nonce="n1",
        service_fee_usd=Decimal("0.05"),
    )
    assert status == 400
    assert "authority_ceiling_usd" in body["error"]


def test_handle_session_request_rejects_a_malformed_decimal(core, capabilities):
    status, body = handle_session_request(
        core, capabilities, body={"agent_id": "buyer", "authority_ceiling_usd": "not-a-number"},
        payment_nonce="n1", service_fee_usd=Decimal("0.05"),
    )
    assert status == 400


def test_handle_session_request_success_shape_never_conflates_fee_and_ceiling(core, capabilities):
    status, body = handle_session_request(
        core, capabilities, body={"agent_id": "buyer", "authority_ceiling_usd": "25.00"},
        payment_nonce="n-shape", service_fee_usd=Decimal("0.05"),
    )
    assert status == 200
    assert body["authority_ceiling_usd"] == "25"
    assert body["service_fee"]["amount_usd"] == "0.05"
    assert body["service_fee"]["status"] == "PAID"
    assert body["root_capability"]["token"] is not None
    assert set(body["root_capability"]["scopes"]) == {
        "read", "reserve", "grant", "consume", "settle", "revoke"
    }
    assert body["newly_claimed"] is True


def test_handle_session_request_duplicate_never_exposes_plaintext_again(core, capabilities):
    status1, body1 = handle_session_request(
        core, capabilities, body={"agent_id": "buyer", "authority_ceiling_usd": "10.00"},
        payment_nonce="n-dup-http", service_fee_usd=Decimal("0.05"),
    )
    status2, body2 = handle_session_request(
        core, capabilities, body={"agent_id": "buyer", "authority_ceiling_usd": "10.00"},
        payment_nonce="n-dup-http", service_fee_usd=Decimal("0.05"),
    )
    assert status1 == 200 and status2 == 200
    assert body1["root_capability"]["token"] is not None
    assert body2["root_capability"] is None
    assert body2["note"] == "session_already_created_and_claimed"
    assert body2["session_id"] == body1["session_id"]


def test_handle_session_request_conflict_returns_409(core, capabilities):
    handle_session_request(
        core, capabilities, body={"agent_id": "buyer", "authority_ceiling_usd": "10.00"},
        payment_nonce="n-conflict-http", service_fee_usd=Decimal("0.05"),
    )
    status, body = handle_session_request(
        core, capabilities, body={"agent_id": "buyer", "authority_ceiling_usd": "999.00"},
        payment_nonce="n-conflict-http", service_fee_usd=Decimal("0.05"),
    )
    assert status == 409
    assert body["error"] == "SessionAuthorizationConflict"


# -- the purchased session can perform the Phase B A2A flow ----------------


def test_the_purchased_root_credential_can_reserve_grant_consume_settle(core, capabilities):
    """Proves a legitimately purchased session's root capability is a
    real, fully-scoped root exactly like `bootstrap.bootstrap_root`
    produces -- usable for the complete Phase B economic lifecycle
    directly against core.py/capabilities.py. The real A2A-transport
    version of this same proof (through a live server subprocess) lives
    in test_a2a_economic_authority_session_service.py."""
    result = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-lifecycle", agent_id="buyer",
        authority_ceiling_usd=Decimal("1.00"), service_fee_usd=Decimal("0.05"),
    )
    session_id = result.session_id
    capabilities.authorize(result.plaintext_token, session_id, "reserve")

    outcome = core.reserve("evt:r1", session_id, "child-of-session", "worker", Decimal("0.40"))
    assert outcome == "created"
    assert core.consume("evt:c1", "child-of-session", Decimal("0.10")) is True
    assert core.settle("evt:s1", "child-of-session", "SUCCESS") is True

    root = core.get(session_id)
    assert root is not None
    assert root.consumed_usd == Decimal("0.10")
    assert root.child_reserved_usd == Decimal("0")
