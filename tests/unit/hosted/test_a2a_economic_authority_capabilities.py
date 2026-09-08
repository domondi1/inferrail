"""Tests for hosted/a2a_economic_authority/capabilities.py's capability-token
store. No network, no A2A/server dependency -- this module is
transport-independent, like core.py, and so are these tests.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTED_DIR = REPO_ROOT / "hosted" / "a2a_economic_authority"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

import pytest  # noqa: E402
from capabilities import (  # noqa: E402
    SCOPES,
    CapabilityStore,
    ExpiredCredential,
    InMemoryCredentialHandoff,
    InsufficientScope,
    InvalidCredential,
    MissingCredential,
    RevokedCredential,
    WrongAuthorizer,
    WrongDelegation,
)


@pytest.fixture
def store(tmp_path) -> CapabilityStore:
    return CapabilityStore(tmp_path / "capabilities.sqlite3")


# -- issuance and successful authorization -------------------------------


def test_issued_token_authorizes_its_own_delegation_and_scope(store: CapabilityStore):
    _token_id, token = store.issue("root", {"read", "consume"})
    info = store.authorize(token, "root", "read")
    assert info.delegation_id == "root"
    assert info.scopes == frozenset({"read", "consume"})


def test_issue_rejects_unknown_scope(store: CapabilityStore):
    with pytest.raises(ValueError):
        store.issue("root", {"read", "not-a-real-scope"})


def test_issue_rejects_empty_scope_set(store: CapabilityStore):
    with pytest.raises(ValueError):
        store.issue("root", set())


def test_all_declared_scopes_are_issuable(store: CapabilityStore):
    _token_id, token = store.issue("root", SCOPES)
    for scope in SCOPES:
        store.authorize(token, "root", scope)  # must not raise


# -- rejection reasons: each is distinct and specific ---------------------


def test_missing_credential_is_rejected(store: CapabilityStore):
    with pytest.raises(MissingCredential):
        store.authorize(None, "root", "read")
    with pytest.raises(MissingCredential):
        store.authorize("", "root", "read")


def test_invalid_credential_is_rejected(store: CapabilityStore):
    store.issue("root", {"read"})
    with pytest.raises(InvalidCredential):
        store.authorize("this-token-was-never-issued", "root", "read")


def test_wrong_delegation_is_rejected(store: CapabilityStore):
    """The central 'a token for one delegation cannot control another' guarantee."""
    _token_id, token = store.issue("child-1", {"read", "consume", "settle"})
    with pytest.raises(WrongDelegation):
        store.authorize(token, "child-2", "read")
    # It still works against the delegation it was actually issued for.
    store.authorize(token, "child-1", "read")


def test_insufficient_scope_is_rejected(store: CapabilityStore):
    _token_id, token = store.issue("root", {"read"})
    with pytest.raises(InsufficientScope):
        store.authorize(token, "root", "revoke")


def test_expired_credential_is_rejected(store: CapabilityStore):
    _token_id, token = store.issue("root", {"read"}, ttl_seconds=0)
    time.sleep(0.01)
    with pytest.raises(ExpiredCredential):
        store.authorize(token, "root", "read")


def test_revoked_credential_is_rejected(store: CapabilityStore):
    _token_id, token = store.issue("root", {"read"})
    store.authorize(token, "root", "read")  # works before revocation
    store.revoke_for_delegations(["root"])
    with pytest.raises(RevokedCredential):
        store.authorize(token, "root", "read")


# -- tree revocation --------------------------------------------------


def test_revoke_for_delegations_revokes_only_named_delegations(store: CapabilityStore):
    _id1, token_a = store.issue("a", {"read"})
    _id2, token_b = store.issue("b", {"read"})
    revoked_count = store.revoke_for_delegations(["a"])
    assert revoked_count == 1
    with pytest.raises(RevokedCredential):
        store.authorize(token_a, "a", "read")
    store.authorize(token_b, "b", "read")  # untouched


def test_revoke_for_delegations_is_idempotent(store: CapabilityStore):
    store.issue("a", {"read"})
    first = store.revoke_for_delegations(["a"])
    second = store.revoke_for_delegations(["a"])
    assert first == 1
    assert second == 0


def test_revoke_for_delegations_empty_list_is_a_safe_no_op(store: CapabilityStore):
    assert store.revoke_for_delegations([]) == 0


# -- durability: only hashes ever touch disk -------------------------------


def test_only_hashes_are_persisted_never_plaintext(tmp_path):
    db_path = tmp_path / "capabilities.sqlite3"
    store = CapabilityStore(db_path)
    _token_id, plaintext = store.issue(
        "root", {"read", "reserve", "grant", "consume", "settle", "revoke"}
    )

    raw_bytes = db_path.read_bytes()
    assert plaintext.encode("utf-8") not in raw_bytes

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT token_hash FROM capability_tokens").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] != plaintext
    assert len(row[0]) == 64  # sha256 hex digest length


def test_capability_state_survives_new_store_instance_against_same_file(tmp_path):
    db_path = tmp_path / "capabilities.sqlite3"
    store1 = CapabilityStore(db_path)
    _token_id, token = store1.issue("root", {"read"})
    store1.revoke_for_delegations(["root"])

    store2 = CapabilityStore(db_path)  # fresh instance, same file -- simulates a restart
    with pytest.raises(RevokedCredential):
        store2.authorize(token, "root", "read")


# -- in-memory credential handoff: the reserve claim-ticket mechanism -----
#
# Redemption fully revalidates the presented credential against a real
# CapabilityStore (existence/expiry/revocation/delegation/scope) instead of
# comparing token hashes, AND requires it to be the EXACT credential that
# authorized the reservation (repair item 5) -- these tests exercise that
# directly, plus the purge-only-when-rightful behavior (repair item 6) and
# the target-delegation-revocation hook (repair item 3).


def _create_claim(
    handoff: InMemoryCredentialHandoff,
    *,
    token_id: str,
    plaintext: str,
    issuer_delegation_id: str,
    issuer_required_scope: str,
    authorizing_token_id: str,
    child_delegation_id: str = "child-x",
) -> str:
    return handoff.create(
        token_id,
        plaintext,
        issuer_delegation_id,
        issuer_required_scope,
        authorizing_token_id=authorizing_token_id,
        child_delegation_id=child_delegation_id,
    )


def test_handoff_redeems_exactly_once_for_the_exact_authorizer(store: CapabilityStore):
    token_id, token = store.issue("root", {"reserve"})
    handoff = InMemoryCredentialHandoff()
    claim_id = _create_claim(
        handoff,
        token_id=token_id,
        plaintext="plaintext-child-token",
        issuer_delegation_id="root",
        issuer_required_scope="reserve",
        authorizing_token_id=token_id,
    )

    assert handoff.redeem(store, claim_id, token) == "plaintext-child-token"

    with pytest.raises(InvalidCredential):
        handoff.redeem(store, claim_id, token)  # already consumed


def test_handoff_rejects_a_different_token_with_the_same_required_scope(store: CapabilityStore):
    """Repair item 5: the claim is bound to the EXACT credential that
    authorized the reservation, not merely to "any token currently holding
    the required scope on the same delegation". A different, equally
    legitimate reserve-scoped credential for the same parent must not be
    able to redeem a reservation it did not itself authorize -- this is
    what closes the "guess an existing child_id, present any reserve-scoped
    token for the parent" vector."""
    authorizing_token_id, authorizing_token = store.issue("root", {"reserve"})
    _other_id, other_token = store.issue("root", {"reserve"})
    handoff = InMemoryCredentialHandoff()
    claim_id = _create_claim(
        handoff,
        token_id=authorizing_token_id,
        plaintext="plaintext-child-token",
        issuer_delegation_id="root",
        issuer_required_scope="reserve",
        authorizing_token_id=authorizing_token_id,
    )

    with pytest.raises(WrongAuthorizer):
        handoff.redeem(store, claim_id, other_token)

    # The claim survives the wrong-authorizer attempt -- the true
    # authorizer can still redeem it.
    assert handoff.redeem(store, claim_id, authorizing_token) == "plaintext-child-token"


def test_handoff_rejects_wrong_scope_but_claim_survives_for_a_legitimate_retry(
    store: CapabilityStore,
):
    """A grant-only token must not redeem a claim that requires 'reserve'
    -- this is the core of the grant/reserve separation (repair item 6)."""
    token_id, grant_only_token = store.issue("root", {"grant"})
    reserve_id, reserve_token = store.issue("root", {"reserve"})
    handoff = InMemoryCredentialHandoff()
    claim_id = _create_claim(
        handoff,
        token_id=reserve_id,
        plaintext="plaintext-child-token",
        issuer_delegation_id="root",
        issuer_required_scope="reserve",
        authorizing_token_id=reserve_id,
    )

    with pytest.raises(InsufficientScope):
        handoff.redeem(store, claim_id, grant_only_token)

    # The claim is untouched by the wrong-scope attempt -- the rightful
    # reserve-scoped holder can still redeem it.
    assert handoff.redeem(store, claim_id, reserve_token) == "plaintext-child-token"
    assert token_id  # sanity: the grant-only token really was minted


def test_handoff_rejects_wrong_delegation(store: CapabilityStore):
    authorizing_id, _token = store.issue("root", {"reserve"})
    _other_id, other_delegation_token = store.issue("child-x", {"reserve"})
    handoff = InMemoryCredentialHandoff()
    claim_id = _create_claim(
        handoff,
        token_id=authorizing_id,
        plaintext="plaintext-child-token",
        issuer_delegation_id="root",
        issuer_required_scope="reserve",
        authorizing_token_id=authorizing_id,
    )

    with pytest.raises(WrongDelegation):
        handoff.redeem(store, claim_id, other_delegation_token)


def test_handoff_rejects_missing_credential(store: CapabilityStore):
    authorizing_id, _token = store.issue("root", {"reserve"})
    handoff = InMemoryCredentialHandoff()
    claim_id = _create_claim(
        handoff,
        token_id=authorizing_id,
        plaintext="plaintext-child-token",
        issuer_delegation_id="root",
        issuer_required_scope="reserve",
        authorizing_token_id=authorizing_id,
    )
    with pytest.raises(MissingCredential):
        handoff.redeem(store, claim_id, None)


def test_handoff_purges_claim_when_the_true_authorizer_is_revoked(store: CapabilityStore):
    """Repair item 3: an outstanding claim must not survive its true
    authorizer being revoked. A revoked authorizer fails redemption AND the
    claim itself is purged (not just left to expire on its own TTL) --
    even a fresh, still-valid-looking token for the same delegation/scope
    cannot redeem it afterward, because the claim_id no longer exists."""
    token_id, token = store.issue("root", {"reserve"})
    handoff = InMemoryCredentialHandoff()
    claim_id = _create_claim(
        handoff,
        token_id=token_id,
        plaintext="plaintext-child-token",
        issuer_delegation_id="root",
        issuer_required_scope="reserve",
        authorizing_token_id=token_id,
    )

    store.revoke_for_delegations(["root"])

    with pytest.raises(RevokedCredential):
        handoff.redeem(store, claim_id, token)

    _new_id, fresh_token = store.issue("root", {"reserve"})
    with pytest.raises(InvalidCredential):
        handoff.redeem(store, claim_id, fresh_token)  # claim_id is gone, not just this token


def test_handoff_purges_claim_when_the_true_authorizer_has_expired(store: CapabilityStore):
    token_id, token = store.issue("root", {"reserve"}, ttl_seconds=0)
    handoff = InMemoryCredentialHandoff()
    claim_id = _create_claim(
        handoff,
        token_id=token_id,
        plaintext="plaintext-child-token",
        issuer_delegation_id="root",
        issuer_required_scope="reserve",
        authorizing_token_id=token_id,
    )
    time.sleep(0.01)

    with pytest.raises(ExpiredCredential):
        handoff.redeem(store, claim_id, token)

    _new_id, fresh_token = store.issue("root", {"reserve"})
    with pytest.raises(InvalidCredential):
        handoff.redeem(store, claim_id, fresh_token)


def test_handoff_unrelated_expired_token_does_not_destroy_the_claim(store: CapabilityStore):
    """Repair item 6's central regression: authorize() checks a presented
    token's OWN expiry/revocation before delegation/scope binding, so an
    UNRELATED expired token (nothing to do with this claim) must not be
    able to purge someone else's still-good claim just by being presented
    against it."""
    authorizing_id, authorizing_token = store.issue("root", {"reserve"})
    _unrelated_id, unrelated_expired_token = store.issue(
        "some-other-delegation-entirely", {"read"}, ttl_seconds=0
    )
    handoff = InMemoryCredentialHandoff()
    claim_id = _create_claim(
        handoff,
        token_id=authorizing_id,
        plaintext="plaintext-child-token",
        issuer_delegation_id="root",
        issuer_required_scope="reserve",
        authorizing_token_id=authorizing_id,
    )
    time.sleep(0.01)

    with pytest.raises(ExpiredCredential):
        handoff.redeem(store, claim_id, unrelated_expired_token)

    # The claim survives -- the true authorizer can still redeem it.
    assert handoff.redeem(store, claim_id, authorizing_token) == "plaintext-child-token"


def test_handoff_unrelated_revoked_token_does_not_destroy_the_claim(store: CapabilityStore):
    authorizing_id, authorizing_token = store.issue("root", {"reserve"})
    _unrelated_id, unrelated_revoked_token = store.issue("some-other-delegation-entirely", {"read"})
    store.revoke_for_delegations(["some-other-delegation-entirely"])
    handoff = InMemoryCredentialHandoff()
    claim_id = _create_claim(
        handoff,
        token_id=authorizing_id,
        plaintext="plaintext-child-token",
        issuer_delegation_id="root",
        issuer_required_scope="reserve",
        authorizing_token_id=authorizing_id,
    )

    with pytest.raises(RevokedCredential):
        handoff.redeem(store, claim_id, unrelated_revoked_token)

    assert handoff.redeem(store, claim_id, authorizing_token) == "plaintext-child-token"


def test_handoff_rejects_expired_claim_itself(store: CapabilityStore):
    token_id, token = store.issue("root", {"reserve"})
    handoff = InMemoryCredentialHandoff(ttl_seconds=0)
    claim_id = _create_claim(
        handoff,
        token_id=token_id,
        plaintext="plaintext-child-token",
        issuer_delegation_id="root",
        issuer_required_scope="reserve",
        authorizing_token_id=token_id,
    )
    time.sleep(0.01)
    with pytest.raises(ExpiredCredential):
        handoff.redeem(store, claim_id, token)


def test_handoff_rejects_unknown_claim_id(store: CapabilityStore):
    handoff = InMemoryCredentialHandoff()
    with pytest.raises(InvalidCredential):
        handoff.redeem(store, "no-such-claim", "any-token")


def test_handoff_is_target_revoked_callback_purges_claim(store: CapabilityStore):
    """Repair item 3: even a fully valid, correctly-scoped, exact-authorizer
    credential must not redeem a claim whose TARGET delegation is
    undergoing tree revocation -- the `is_target_revoked` callback closes
    this window, which exists between `mark_revocation_started` and the
    rest of a tree teardown finishing."""
    authorizing_id, authorizing_token = store.issue("root", {"reserve"})
    handoff = InMemoryCredentialHandoff()
    claim_id = _create_claim(
        handoff,
        token_id=authorizing_id,
        plaintext="plaintext-child-token",
        issuer_delegation_id="root",
        issuer_required_scope="reserve",
        authorizing_token_id=authorizing_id,
        child_delegation_id="child-under-revocation",
    )

    with pytest.raises(RevokedCredential):
        handoff.redeem(
            store, claim_id, authorizing_token, is_target_revoked=lambda _did: True
        )

    # Purged -- gone even to a request that would no longer say "revoked".
    with pytest.raises(InvalidCredential):
        handoff.redeem(store, claim_id, authorizing_token, is_target_revoked=lambda _did: False)


def test_handoff_is_target_revoked_callback_allows_redemption_when_false(store: CapabilityStore):
    authorizing_id, authorizing_token = store.issue("root", {"reserve"})
    handoff = InMemoryCredentialHandoff()
    claim_id = _create_claim(
        handoff,
        token_id=authorizing_id,
        plaintext="plaintext-child-token",
        issuer_delegation_id="root",
        issuer_required_scope="reserve",
        authorizing_token_id=authorizing_id,
        child_delegation_id="child-not-revoked",
    )
    assert (
        handoff.redeem(store, claim_id, authorizing_token, is_target_revoked=lambda _did: False)
        == "plaintext-child-token"
    )


def test_purge_for_delegations_removes_only_matching_claims(store: CapabilityStore):
    token_a, _plaintext_a = store.issue("root", {"reserve"})
    token_b, plaintext_b = store.issue("child-x", {"reserve"})
    handoff = InMemoryCredentialHandoff()
    claim_a = _create_claim(
        handoff,
        token_id=token_a,
        plaintext="plaintext-a",
        issuer_delegation_id="root",
        issuer_required_scope="reserve",
        authorizing_token_id=token_a,
        child_delegation_id="grandchild-a",
    )
    claim_b = _create_claim(
        handoff,
        token_id=token_b,
        plaintext="plaintext-b",
        issuer_delegation_id="child-x",
        issuer_required_scope="reserve",
        authorizing_token_id=token_b,
        child_delegation_id="grandchild-b",
    )

    purged = handoff.purge_for_delegations(["root"])
    assert purged == 1

    with pytest.raises(InvalidCredential):
        handoff.redeem(store, claim_a, "irrelevant")

    assert handoff.redeem(store, claim_b, plaintext_b) == "plaintext-b"


def test_purge_for_delegations_empty_list_is_a_safe_no_op():
    handoff = InMemoryCredentialHandoff()
    assert handoff.purge_for_delegations([]) == 0


def test_concurrent_redemption_of_the_same_claim_succeeds_exactly_once(store: CapabilityStore):
    """Repair item 3's concurrency requirement: race many threads trying
    to redeem the same claim_id. Exactly one must receive the plaintext
    token; every other attempt must fail cleanly (never with a partial or
    duplicated credential)."""
    from concurrent.futures import ThreadPoolExecutor

    token_id, token = store.issue("root", {"reserve"})
    handoff = InMemoryCredentialHandoff()
    claim_id = _create_claim(
        handoff,
        token_id=token_id,
        plaintext="the-only-plaintext-token",
        issuer_delegation_id="root",
        issuer_required_scope="reserve",
        authorizing_token_id=token_id,
    )

    def try_redeem(_i: int) -> str | None:
        try:
            return handoff.redeem(store, claim_id, token)
        except InvalidCredential:
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(try_redeem, range(16)))

    successes = [r for r in results if r is not None]
    assert successes == ["the-only-plaintext-token"], (
        "exactly one concurrent redemption must succeed"
    )
    assert results.count(None) == 15


# -- token_id_for and scopes_issued_for: plain lookups used above ---------


def test_token_id_for_works_regardless_of_revoked_or_expired(store: CapabilityStore):
    token_id, token = store.issue("root", {"read"})
    assert store.token_id_for(token) == token_id
    store.revoke_for_delegations(["root"])
    assert store.token_id_for(token) == token_id  # still resolvable after revocation


def test_token_id_for_returns_none_for_unknown_or_missing_token(store: CapabilityStore):
    assert store.token_id_for(None) is None
    assert store.token_id_for("not-a-real-token") is None


def test_scopes_issued_for_returns_first_issued_scopes(store: CapabilityStore):
    _id1, _t1 = store.issue("root", {"reserve"})
    assert store.scopes_issued_for("root") == frozenset({"reserve"})


def test_scopes_issued_for_returns_none_when_nothing_issued(store: CapabilityStore):
    assert store.scopes_issued_for("never-issued") is None


# -- exclusions: no outbound network calls in this module ------------------


def test_no_outbound_network_calls_or_a2a_dependency_in_capabilities_module():
    source = (HOSTED_DIR / "capabilities.py").read_text()
    for forbidden_import in ("httpx", "requests", "socket", "urllib", "a2a"):
        assert forbidden_import not in source, f"unexpected dependency: {forbidden_import}"
