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


def test_handoff_redeems_exactly_once_and_binds_to_issuer_token():
    handoff = InMemoryCredentialHandoff()
    claim_id = handoff.create("token-id-1", "plaintext-child-token", "issuer-token")

    assert handoff.redeem(claim_id, "issuer-token") == "plaintext-child-token"

    with pytest.raises(InvalidCredential):
        handoff.redeem(claim_id, "issuer-token")  # already consumed


def test_handoff_rejects_wrong_presented_credential():
    handoff = InMemoryCredentialHandoff()
    claim_id = handoff.create("token-id-1", "plaintext-child-token", "issuer-token")
    with pytest.raises(WrongDelegation):
        handoff.redeem(claim_id, "someone-elses-token")
    # The claim survives a failed redemption attempt with the wrong credential...
    assert handoff.redeem(claim_id, "issuer-token") == "plaintext-child-token"


def test_handoff_rejects_missing_credential():
    handoff = InMemoryCredentialHandoff()
    claim_id = handoff.create("token-id-1", "plaintext-child-token", "issuer-token")
    with pytest.raises(MissingCredential):
        handoff.redeem(claim_id, None)


def test_handoff_rejects_expired_claim():
    handoff = InMemoryCredentialHandoff(ttl_seconds=0)
    claim_id = handoff.create("token-id-1", "plaintext-child-token", "issuer-token")
    time.sleep(0.01)
    with pytest.raises(ExpiredCredential):
        handoff.redeem(claim_id, "issuer-token")


def test_handoff_rejects_unknown_claim_id():
    handoff = InMemoryCredentialHandoff()
    with pytest.raises(InvalidCredential):
        handoff.redeem("no-such-claim", "any-token")


# -- exclusions: no outbound network calls in this module ------------------


def test_no_outbound_network_calls_or_a2a_dependency_in_capabilities_module():
    source = (HOSTED_DIR / "capabilities.py").read_text()
    for forbidden_import in ("httpx", "requests", "socket", "urllib", "a2a"):
        assert forbidden_import not in source, f"unexpected dependency: {forbidden_import}"
