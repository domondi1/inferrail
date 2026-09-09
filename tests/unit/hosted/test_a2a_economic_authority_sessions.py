"""Tests for hosted/a2a_economic_authority/sessions.py's paid-session
creation logic (Phase C). No network, no x402/FastAPI/facilitator
dependency -- like core.py and capabilities.py, this module is
transport-independent and so are these tests: every test here calls
`create_or_recover_session`/`handle_session_request` directly with a
synthetic `payment_nonce`, exactly as `server.py`'s route calls it AFTER
the x402 middleware has already verified a real payment. The x402/HTTP
wiring itself is covered separately in
`test_a2a_economic_authority_session_service.py`.

Also covers the payment-security review that replaced `session_id`-keyed
recovery with `payment_nonce`-keyed recovery and made
`recovery_secret_hash` required (see `sessions.py`'s and
`capabilities.py`'s module/method docstrings for the full defect and
guarantee): `create_or_recover_session`/`handle_session_request` now
reject a purchase attempt with no `recovery_secret_hash`, and
`recover_session`/`handle_session_recovery_request` identify the purchase
by `payment_nonce` -- information the buyer's own x402 client generates
and holds before ever paying -- not the server-generated `session_id`.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import subprocess
import sys
import threading
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTED_DIR = REPO_ROOT / "hosted" / "a2a_economic_authority"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

SESSION_CRASH_HELPER = (
    Path(__file__).resolve().parent / "_a2a_economic_authority_session_purchase_crash_helper.py"
)

import pytest  # noqa: E402
from capabilities import (  # noqa: E402
    CapabilityStore,
    InvalidRecoverySecret,
    SessionAuthorizationConflict,
)
from core import EconomicAuthorityStore  # noqa: E402
from sessions import (  # noqa: E402
    create_or_recover_session,
    handle_session_recovery_request,
    handle_session_request,
    recover_session,
)

_HASH = "a" * 64  # a syntactically valid (but fixed) recovery_secret_hash for tests that
# never exercise recovery itself -- see `_recovery_pair()` for tests that do.


def _recovery_pair() -> tuple[str, str]:
    """Returns (recovery_secret, recovery_secret_hash) exactly as a real
    buyer's client would compute it: a high-entropy secret generated and
    hashed locally, before ever paying."""
    secret = secrets.token_urlsafe(32)
    return secret, hashlib.sha256(secret.encode("utf-8")).hexdigest()


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
        recovery_secret_hash=_HASH,
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
        recovery_secret_hash=_HASH,
    )
    assert result.authority_ceiling_usd == Decimal("500.00")
    assert result.service_fee_usd == Decimal("0.05")
    root = core.get(result.session_id)
    assert root is not None
    assert root.authority_usd == Decimal("500.00"), (
        "the root's real authority must be the ceiling, never the service fee"
    )


# -- recovery_secret_hash is required, not optional -------------------------


def test_create_or_recover_session_rejects_a_missing_recovery_secret_hash(core, capabilities):
    """An agent-first paid product cannot honestly guarantee recovery for
    a purchase that opted out of the one mechanism that makes it
    possible -- see `create_or_recover_session`'s docstring. Rejected
    before any economic state is touched."""
    with pytest.raises(ValueError, match="recovery_secret_hash"):
        create_or_recover_session(
            core, capabilities, payment_nonce="nonce-no-secret", agent_id="buyer",
            authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
            recovery_secret_hash=None,  # type: ignore[arg-type]
        )
    assert capabilities.get_session_purchase("nonce-no-secret") is None
    assert core.get("nonce-no-secret") is None


def test_create_or_recover_session_rejects_a_malformed_recovery_secret_hash(core, capabilities):
    with pytest.raises(ValueError, match="recovery_secret_hash"):
        create_or_recover_session(
            core, capabilities, payment_nonce="nonce-bad-secret", agent_id="buyer",
            authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
            recovery_secret_hash="not-a-hex-sha256",
        )


def test_handle_session_request_rejects_a_missing_recovery_secret_hash(core, capabilities):
    status, body = handle_session_request(
        core, capabilities, body={"agent_id": "buyer", "authority_ceiling_usd": "10.00"},
        payment_nonce="n-no-secret-http", service_fee_usd=Decimal("0.05"),
    )
    assert status == 400
    assert "recovery_secret_hash" in body["error"]
    assert capabilities.get_session_purchase("n-no-secret-http") is None


# -- ordinary duplicate delivery is idempotent ------------------------------


def test_replaying_the_same_payment_nonce_does_not_recharge_or_recreate(core, capabilities):
    first = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-dup", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=_HASH,
    )
    second = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-dup", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=_HASH,
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
            recovery_secret_hash=_HASH,
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
                    recovery_secret_hash=_HASH,
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
        recovery_secret_hash=_HASH,
    )
    with pytest.raises(SessionAuthorizationConflict):
        create_or_recover_session(
            core, capabilities, payment_nonce="nonce-conflict", agent_id="buyer",
            authority_ceiling_usd=Decimal("999.00"), service_fee_usd=Decimal("0.05"),
            recovery_secret_hash=_HASH,
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
        recovery_secret_hash=_HASH,
    )
    with pytest.raises(SessionAuthorizationConflict):
        create_or_recover_session(
            core, capabilities, payment_nonce="nonce-conflict-agent", agent_id="buyer-b",
            authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
            recovery_secret_hash=_HASH,
        )


def test_a_different_nonce_always_creates_a_genuinely_different_session(core, capabilities):
    r1 = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-a", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=_HASH,
    )
    r2 = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-b", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=_HASH,
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
        recovery_secret_hash=_HASH,
    )

    # Simulates a process restart: brand new store instances, same files.
    core2 = EconomicAuthorityStore(db_path)
    caps2 = CapabilityStore(cap_path)
    second = create_or_recover_session(
        core2, caps2, payment_nonce="nonce-restart", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=_HASH,
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
            recovery_secret_hash=_HASH,
        )
    with pytest.raises(ValueError):
        create_or_recover_session(
            core, capabilities, payment_nonce="nonce-neg2", agent_id="buyer",
            authority_ceiling_usd=Decimal("-5.00"), service_fee_usd=Decimal("0.05"),
            recovery_secret_hash=_HASH,
        )


def test_non_finite_authority_ceiling_is_rejected(core, capabilities):
    with pytest.raises(ValueError):
        create_or_recover_session(
            core, capabilities, payment_nonce="nonce-nan", agent_id="buyer",
            authority_ceiling_usd=Decimal("NaN"), service_fee_usd=Decimal("0.05"),
            recovery_secret_hash=_HASH,
        )


def test_empty_agent_id_is_rejected(core, capabilities):
    with pytest.raises(ValueError):
        create_or_recover_session(
            core, capabilities, payment_nonce="nonce-empty-agent", agent_id="",
            authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
            recovery_secret_hash=_HASH,
        )


def test_equivalent_decimal_strings_do_not_false_conflict(core, capabilities):
    """"10" and "10.00" must be recognized as the same amount on retry --
    matching core.py's own canonical-decimal-comparison discipline."""
    create_or_recover_session(
        core, capabilities, payment_nonce="nonce-canon", agent_id="buyer",
        authority_ceiling_usd=Decimal("10"), service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=_HASH,
    )
    result = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-canon", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.050"),
        recovery_secret_hash=_HASH,
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
        core, capabilities, body={"authority_ceiling_usd": "10.00", "recovery_secret_hash": _HASH},
        payment_nonce="n1", service_fee_usd=Decimal("0.05"),
    )
    assert status == 400
    assert "agent_id" in body["error"]


def test_handle_session_request_requires_authority_ceiling_usd(core, capabilities):
    status, body = handle_session_request(
        core, capabilities, body={"agent_id": "buyer", "recovery_secret_hash": _HASH},
        payment_nonce="n1", service_fee_usd=Decimal("0.05"),
    )
    assert status == 400
    assert "authority_ceiling_usd" in body["error"]


def test_handle_session_request_rejects_a_malformed_decimal(core, capabilities):
    status, body = handle_session_request(
        core, capabilities,
        body={
            "agent_id": "buyer", "authority_ceiling_usd": "not-a-number",
            "recovery_secret_hash": _HASH,
        },
        payment_nonce="n1", service_fee_usd=Decimal("0.05"),
    )
    assert status == 400


def test_handle_session_request_success_shape_never_conflates_fee_and_ceiling(core, capabilities):
    status, body = handle_session_request(
        core, capabilities,
        body={
            "agent_id": "buyer", "authority_ceiling_usd": "25.00",
            "recovery_secret_hash": _HASH,
        },
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
        core, capabilities,
        body={
            "agent_id": "buyer", "authority_ceiling_usd": "10.00",
            "recovery_secret_hash": _HASH,
        },
        payment_nonce="n-dup-http", service_fee_usd=Decimal("0.05"),
    )
    status2, body2 = handle_session_request(
        core, capabilities,
        body={
            "agent_id": "buyer", "authority_ceiling_usd": "10.00",
            "recovery_secret_hash": _HASH,
        },
        payment_nonce="n-dup-http", service_fee_usd=Decimal("0.05"),
    )
    assert status1 == 200 and status2 == 200
    assert body1["root_capability"]["token"] is not None
    assert body2["root_capability"] is None
    assert body2["note"] == "session_already_created_and_claimed"
    assert body2["session_id"] == body1["session_id"]


def test_handle_session_request_conflict_returns_409(core, capabilities):
    handle_session_request(
        core, capabilities,
        body={
            "agent_id": "buyer", "authority_ceiling_usd": "10.00",
            "recovery_secret_hash": _HASH,
        },
        payment_nonce="n-conflict-http", service_fee_usd=Decimal("0.05"),
    )
    status, body = handle_session_request(
        core, capabilities,
        body={
            "agent_id": "buyer", "authority_ceiling_usd": "999.00",
            "recovery_secret_hash": _HASH,
        },
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
        recovery_secret_hash=_HASH,
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


# ===========================================================================
# Recovery: payment_nonce-keyed, buyer-side-information-only, and covering
# the specific crash windows between a settled payment and a fully
# materialized session (see sessions.py's module docstring).
# ===========================================================================


def test_recovery_uses_only_information_the_buyer_held_before_payment(core, capabilities):
    """The full happy path: a buyer generates their own recovery secret
    and payment_nonce before ever paying (this test never even looks at
    the purchase response's session_id before calling recover_session,
    proving it is not needed)."""
    secret, secret_hash = _recovery_pair()
    result = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-recover-1", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=secret_hash,
    )
    original_token = result.plaintext_token
    assert original_token is not None

    purchase, _token_id, new_token = recover_session(
        core, capabilities, payment_nonce="nonce-recover-1", recovery_secret=secret,
    )
    assert purchase.session_id == result.session_id
    assert new_token != original_token
    assert len(_live_token_ids(capabilities, result.session_id)) == 1
    with pytest.raises(Exception):  # noqa: B017 - old token must be dead
        capabilities.authorize(original_token, result.session_id, "reserve")
    info = capabilities.authorize(new_token, result.session_id, "reserve")
    assert info.delegation_id == result.session_id

    root = core.get(result.session_id)
    assert root is not None
    assert root.authority_usd == Decimal("10.00"), "recovery must never change the economics"


def test_recovery_rejects_the_wrong_secret_and_changes_nothing(core, capabilities):
    secret, secret_hash = _recovery_pair()
    result = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-recover-wrong", agent_id="buyer",
        authority_ceiling_usd=Decimal("5.00"), service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=secret_hash,
    )
    with pytest.raises(InvalidRecoverySecret):
        recover_session(
            core, capabilities, payment_nonce="nonce-recover-wrong",
            recovery_secret="not-the-right-secret",
        )
    # The original credential is still the one live -- an unrelated caller
    # presenting the wrong secret must never rotate it away.
    assert len(_live_token_ids(capabilities, result.session_id)) == 1
    info = capabilities.authorize(result.plaintext_token, result.session_id, "reserve")
    assert info.delegation_id == result.session_id


def test_recovery_rejects_an_unrelated_payment_nonce(core, capabilities):
    """An unrelated caller who never paid, guessing at (or observing) a
    payment_nonce that was never actually purchased -- or presenting the
    right secret against the WRONG nonce -- gets nothing."""
    secret, secret_hash = _recovery_pair()
    create_or_recover_session(
        core, capabilities, payment_nonce="nonce-real-purchase", agent_id="buyer",
        authority_ceiling_usd=Decimal("5.00"), service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=secret_hash,
    )
    with pytest.raises(InvalidRecoverySecret):
        recover_session(
            core, capabilities, payment_nonce="never-purchased-nonce", recovery_secret=secret,
        )
    with pytest.raises(InvalidRecoverySecret):
        recover_session(
            core, capabilities, payment_nonce="never-purchased-nonce",
            recovery_secret=secrets.token_urlsafe(32),
        )


def test_knowing_the_payment_nonce_alone_is_never_sufficient_authorization(core, capabilities):
    """payment_nonce is not secret (see capabilities.py's docstring: it
    travels in the buyer's own signed payload, is disclosed to the
    facilitator, and is derivable from the on-chain transfer once
    settled) -- an unrelated caller who knows a real payment_nonce but
    not the matching recovery_secret must be rejected exactly like a
    caller who knows nothing at all."""
    secret, secret_hash = _recovery_pair()
    result = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-observed", agent_id="buyer",
        authority_ceiling_usd=Decimal("5.00"), service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=secret_hash,
    )
    with pytest.raises(InvalidRecoverySecret):
        recover_session(
            core, capabilities, payment_nonce="nonce-observed",
            recovery_secret=secrets.token_urlsafe(32),
        )
    assert len(_live_token_ids(capabilities, result.session_id)) == 1


def test_recovery_completes_a_purchase_whose_root_was_never_created(core, capabilities):
    """Simulates failure window 2: the durable purchase record exists
    (`record_session_purchase` committed) but the process crashed before
    `core.create_root` ever ran -- e.g. between the two separate SQLite
    files this module writes to. Recovery must notice the root is
    missing, create it (idempotently, from the durably recorded
    agent_id/authority_ceiling_usd), and still mint a working
    credential -- not merely assume the root already exists."""
    secret, secret_hash = _recovery_pair()
    purchase = capabilities.record_session_purchase(
        "nonce-crash-before-root", "buyer", "7.50", "0.05", recovery_secret_hash=secret_hash,
    )
    assert core.get(purchase.session_id) is None, "precondition: root genuinely does not exist yet"

    result_purchase, _token_id, plaintext = recover_session(
        core, capabilities, payment_nonce="nonce-crash-before-root", recovery_secret=secret,
    )
    assert result_purchase.session_id == purchase.session_id

    root = core.get(purchase.session_id)
    assert root is not None, "recovery must complete the missing root creation step"
    assert root.agent_id == "buyer"
    assert root.authority_usd == Decimal("7.50")

    info = capabilities.authorize(plaintext, purchase.session_id, "reserve")
    assert info.delegation_id == purchase.session_id
    assert len(_live_token_ids(capabilities, purchase.session_id)) == 1


def test_recovery_completes_a_purchase_whose_credential_was_never_minted(core, capabilities):
    """Simulates failure window 3: the purchase row AND the root
    delegation both exist, but the process crashed before
    `issue_or_rotate_session_credential` ever ran (`claimed` still 0, no
    capability_tokens row at all). Recovery must still work even though
    there is no prior live token to revoke."""
    secret, secret_hash = _recovery_pair()
    purchase = capabilities.record_session_purchase(
        "nonce-crash-before-credential", "buyer", "3.00", "0.05",
        recovery_secret_hash=secret_hash,
    )
    core.create_root(
        event_id=f"session-root:{purchase.payment_nonce}",
        delegation_id=purchase.session_id,
        agent_id="buyer",
        envelope_usd=Decimal("3.00"),
    )
    assert len(_live_token_ids(capabilities, purchase.session_id)) == 0, (
        "precondition: no credential has ever been minted yet"
    )

    _purchase, _token_id, plaintext = recover_session(
        core, capabilities, payment_nonce="nonce-crash-before-credential", recovery_secret=secret,
    )
    info = capabilities.authorize(plaintext, purchase.session_id, "reserve")
    assert info.delegation_id == purchase.session_id
    assert len(_live_token_ids(capabilities, purchase.session_id)) == 1

    reread = capabilities.get_session_purchase("nonce-crash-before-credential")
    assert reread is not None
    assert reread.claimed is True, (
        "recovery must mark the purchase claimed so a later ordinary-path "
        "mint (were one ever reachable again) can never double-issue"
    )


def test_settlement_never_reached_means_recovery_is_correctly_refused(core, capabilities):
    """Simulates failure window 1: the process crashed before
    `record_session_purchase` ever committed -- no durable state of any
    kind exists for this payment_nonce. Recovery must refuse it rather
    than fabricate a session for a request it cannot prove ever reached
    this module (the required economic guarantee: a failed or unsettled
    payment must never obtain a usable session)."""
    secret, _secret_hash = _recovery_pair()
    with pytest.raises(InvalidRecoverySecret):
        recover_session(
            core, capabilities, payment_nonce="nonce-never-recorded", recovery_secret=secret,
        )
    assert capabilities.get_session_purchase("nonce-never-recorded") is None
    assert core.get("nonce-never-recorded") is None


def test_repeated_recovery_calls_always_leave_exactly_one_live_credential(core, capabilities):
    """A buyer may legitimately call recovery more than once (e.g. they
    lost the response again). Each call must succeed and mint a fresh
    credential, but at most one may ever be live at a time."""
    secret, secret_hash = _recovery_pair()
    result = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-recover-repeat", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=secret_hash,
    )
    tokens = [result.plaintext_token]
    for _ in range(3):
        _purchase, _token_id, plaintext = recover_session(
            core, capabilities, payment_nonce="nonce-recover-repeat", recovery_secret=secret,
        )
        tokens.append(plaintext)

    assert len(set(tokens)) == len(tokens), "every recovery must mint a genuinely fresh credential"
    assert len(_live_token_ids(capabilities, result.session_id)) == 1
    # Only the most recent token is live.
    info = capabilities.authorize(tokens[-1], result.session_id, "reserve")
    assert info.delegation_id == result.session_id


def test_concurrent_recovery_calls_with_the_correct_secret_leave_exactly_one_live_credential(
    core, capabilities
):
    secret, secret_hash = _recovery_pair()
    result = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-recover-concurrent", agent_id="buyer",
        authority_ceiling_usd=Decimal("10.00"), service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=secret_hash,
    )

    results: list = []
    errors: list = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        try:
            results.append(
                recover_session(
                    core, capabilities, payment_nonce="nonce-recover-concurrent",
                    recovery_secret=secret,
                )
            )
        except Exception as exc:  # pragma: no cover - failure path only
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == 4
    assert len(_live_token_ids(capabilities, result.session_id)) == 1
    root = core.get(result.session_id)
    assert root is not None
    assert root.authority_usd == Decimal("10.00")


def test_concurrent_purchase_and_recovery_never_double_reserves_or_leaves_two_live_tokens(
    core, capabilities
):
    """A purchase retry (ordinary duplicate delivery) racing against a
    legitimate recovery call for the SAME payment_nonce must still end
    with exactly one live credential and unchanged economics."""
    secret, secret_hash = _recovery_pair()
    result = create_or_recover_session(
        core, capabilities, payment_nonce="nonce-purchase-vs-recovery", agent_id="buyer",
        authority_ceiling_usd=Decimal("6.00"), service_fee_usd=Decimal("0.05"),
        recovery_secret_hash=secret_hash,
    )

    errors: list = []
    barrier = threading.Barrier(2)

    def purchase_retry():
        barrier.wait()
        try:
            create_or_recover_session(
                core, capabilities, payment_nonce="nonce-purchase-vs-recovery", agent_id="buyer",
                authority_ceiling_usd=Decimal("6.00"), service_fee_usd=Decimal("0.05"),
                recovery_secret_hash=secret_hash,
            )
        except Exception as exc:  # pragma: no cover - failure path only
            errors.append(exc)

    def recovery_call():
        barrier.wait()
        try:
            recover_session(
                core, capabilities, payment_nonce="nonce-purchase-vs-recovery",
                recovery_secret=secret,
            )
        except Exception as exc:  # pragma: no cover - failure path only
            errors.append(exc)

    threads = [threading.Thread(target=purchase_retry), threading.Thread(target=recovery_call)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(_live_token_ids(capabilities, result.session_id)) == 1
    root = core.get(result.session_id)
    assert root is not None
    assert root.authority_usd == Decimal("6.00")


def _run_session_crash_helper(
    db_path: Path, cap_db_path: Path, payment_nonce: str, recovery_secret_hash: str, boundary: str
) -> None:
    result = subprocess.run(
        [
            sys.executable, str(SESSION_CRASH_HELPER), str(db_path), str(cap_db_path),
            payment_nonce, recovery_secret_hash, boundary,
        ],
        capture_output=True, text=True, timeout=15,
    )
    # os._exit(1) means the process never returns 0 -- the crash is real,
    # not a clean shutdown that happens to also mutate state.
    assert result.returncode != 0, (
        f"helper for {boundary!r} exited cleanly (code {result.returncode}); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_recovery_survives_a_real_process_kill_before_the_root_was_created(tmp_path):
    """Failure window 2, with a genuine `os._exit` hard kill (not a
    simulated gap) between `record_session_purchase` committing and
    `core.create_root` ever running -- then a brand-new process (fresh
    store instances against the same files, exactly like a real restart)
    recovers the session using only the payment_nonce/recovery_secret a
    buyer would have held from before payment."""
    db_path = tmp_path / "authority.sqlite3"
    cap_db_path = tmp_path / "capabilities.sqlite3"
    secret, secret_hash = _recovery_pair()

    _run_session_crash_helper(
        db_path, cap_db_path, "nonce-real-crash-before-root", secret_hash,
        "after_purchase_record",
    )

    # Fresh instances, as a real restarted process would open.
    core = EconomicAuthorityStore(db_path)
    capabilities = CapabilityStore(cap_db_path)
    purchase_before_recovery = capabilities.get_session_purchase("nonce-real-crash-before-root")
    assert purchase_before_recovery is not None
    assert core.get(purchase_before_recovery.session_id) is None, (
        "precondition: the real crash genuinely happened before create_root ran"
    )

    purchase, _token_id, plaintext = recover_session(
        core, capabilities, payment_nonce="nonce-real-crash-before-root", recovery_secret=secret,
    )
    root = core.get(purchase.session_id)
    assert root is not None
    assert root.authority_usd == Decimal("10.00")
    info = capabilities.authorize(plaintext, purchase.session_id, "reserve")
    assert info.delegation_id == purchase.session_id


def test_recovery_survives_a_real_process_kill_before_the_credential_was_issued(tmp_path):
    """Failure window 3, with a genuine `os._exit` hard kill between
    `core.create_root` committing and `issue_or_rotate_session_credential`
    ever running."""
    db_path = tmp_path / "authority.sqlite3"
    cap_db_path = tmp_path / "capabilities.sqlite3"
    secret, secret_hash = _recovery_pair()

    _run_session_crash_helper(
        db_path, cap_db_path, "nonce-real-crash-before-credential", secret_hash,
        "after_root_created",
    )

    core = EconomicAuthorityStore(db_path)
    capabilities = CapabilityStore(cap_db_path)
    purchase = capabilities.get_session_purchase("nonce-real-crash-before-credential")
    assert purchase is not None
    assert core.get(purchase.session_id) is not None, "precondition: the root was created"
    assert len(_live_token_ids(capabilities, purchase.session_id)) == 0, (
        "precondition: the real crash genuinely happened before any credential was issued"
    )

    _purchase, _token_id, plaintext = recover_session(
        core, capabilities, payment_nonce="nonce-real-crash-before-credential",
        recovery_secret=secret,
    )
    info = capabilities.authorize(plaintext, purchase.session_id, "reserve")
    assert info.delegation_id == purchase.session_id
    assert len(_live_token_ids(capabilities, purchase.session_id)) == 1


def test_a_real_process_kill_before_any_purchase_record_leaves_recovery_correctly_refused(
    tmp_path,
):
    """Failure window 1, characterized with a genuine process kill: there
    is no step to crash mid-way through, because this boundary is BEFORE
    the first durable write this module ever makes (standing in for a
    crash between x402's real settlement succeeding and
    `record_session_purchase` ever running -- see sessions.py's module
    docstring, "Known residual gap"). No subprocess is spawned here since
    there is nothing for it to do before crashing; this test instead
    proves the other side of the guarantee directly: recovery for a
    payment_nonce with no durable state must be refused, never
    fabricated."""
    db_path = tmp_path / "authority.sqlite3"
    cap_db_path = tmp_path / "capabilities.sqlite3"
    secret, _secret_hash = _recovery_pair()
    core = EconomicAuthorityStore(db_path)
    capabilities = CapabilityStore(cap_db_path)

    with pytest.raises(InvalidRecoverySecret):
        recover_session(
            core, capabilities, payment_nonce="nonce-crashed-before-any-write",
            recovery_secret=secret,
        )
    assert capabilities.get_session_purchase("nonce-crashed-before-any-write") is None


# -- handle_session_recovery_request: the transport-adjacent handler -------


def test_handle_session_recovery_request_requires_payment_nonce(core, capabilities):
    status, body = handle_session_recovery_request(
        core, capabilities, body={"recovery_secret": "whatever"},
    )
    assert status == 400
    assert "payment_nonce" in body["error"]


def test_handle_session_recovery_request_requires_recovery_secret(core, capabilities):
    status, body = handle_session_recovery_request(
        core, capabilities, body={"payment_nonce": "whatever"},
    )
    assert status == 400
    assert "recovery_secret" in body["error"]


def test_handle_session_recovery_request_full_round_trip(core, capabilities):
    secret, secret_hash = _recovery_pair()
    status1, body1 = handle_session_request(
        core, capabilities,
        body={
            "agent_id": "buyer", "authority_ceiling_usd": "10.00",
            "recovery_secret_hash": secret_hash,
        },
        payment_nonce="n-recover-http", service_fee_usd=Decimal("0.05"),
    )
    assert status1 == 200
    session_id = body1["session_id"]
    original_token = body1["root_capability"]["token"]

    status2, body2 = handle_session_recovery_request(
        core, capabilities,
        body={"payment_nonce": "n-recover-http", "recovery_secret": secret},
    )
    assert status2 == 200, body2
    assert body2["session_id"] == session_id
    new_token = body2["root_capability"]["token"]
    assert new_token != original_token

    info = capabilities.authorize(new_token, session_id, "reserve")
    assert info.delegation_id == session_id
    with pytest.raises(Exception):  # noqa: B017 - old token must be dead
        capabilities.authorize(original_token, session_id, "reserve")


def test_handle_session_recovery_request_rejects_wrong_secret_with_403(core, capabilities):
    secret, secret_hash = _recovery_pair()
    handle_session_request(
        core, capabilities,
        body={
            "agent_id": "buyer", "authority_ceiling_usd": "10.00",
            "recovery_secret_hash": secret_hash,
        },
        payment_nonce="n-recover-http-wrong", service_fee_usd=Decimal("0.05"),
    )
    status, body = handle_session_recovery_request(
        core, capabilities,
        body={"payment_nonce": "n-recover-http-wrong", "recovery_secret": "wrong-secret"},
    )
    assert status == 403
    assert body["error"] == "InvalidRecoverySecret"
