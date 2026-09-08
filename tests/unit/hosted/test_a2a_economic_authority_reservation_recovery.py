"""Crash-safe child credential recovery (finding 1).

Covers the exact bug an independent review surfaced against
`hosted/a2a_economic_authority`: the sequence `core.reserve()` commits ->
`capabilities.issue()` mints a child capability -> the plaintext is
handed to `InMemoryCredentialHandoff` -> a claim_id is returned, spans two
separate SQLite databases and one process-local, non-durable buffer. A
crash or lost response between any of these steps could previously leave
a real economic reservation and a real (or non-existent) capability with
no way for the caller to ever obtain a usable credential -- a matching
retry returned `credential_claim_id: None` unconditionally.

These tests hard-kill a real subprocess (`os._exit`, which skips atexit
handlers and buffered output -- see
`_a2a_economic_authority_reserve_recovery_crash_helper.py`) at each of
three granular boundaries, then reopen fresh store instances against the
same database files -- a real restart -- and drive the exact recovery
logic `executor._finish_reserve` uses (see that module for the live,
full-transport version of the same checks, exercised in
`test_a2a_economic_authority_transport.py`) directly against
`core.py`/`capabilities.py`'s public API. No network, no A2A/a2a-sdk
dependency -- this module is transport-independent and so are these
tests, so they run in every CI job, not only the one that installs the
hosted extra.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTED_DIR = REPO_ROOT / "hosted" / "a2a_economic_authority"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

from capabilities import CapabilityStore, InMemoryCredentialHandoff  # noqa: E402
from core import EconomicAuthorityStore  # noqa: E402

CRASH_HELPER = (
    Path(__file__).resolve().parent / "_a2a_economic_authority_reserve_recovery_crash_helper.py"
)
CHILD_DELEGATION_ID = "child-recovery-crash"
CHILD_SCOPES = frozenset({"read", "consume", "settle"})

BOUNDARIES = ("after_economic_reserve", "after_capability_issuance", "after_claim_created")


def _run_helper(db_path: Path, cap_db_path: Path, state_json_path: Path, boundary: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(CRASH_HELPER),
            str(db_path),
            str(cap_db_path),
            str(state_json_path),
            boundary,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    # os._exit(1) means the process never returns 0 -- the crash is real,
    # not a clean shutdown that happens to also mutate state.
    assert result.returncode != 0, (
        f"helper for {boundary!r} exited cleanly (code {result.returncode}); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def _recover(
    core_store: EconomicAuthorityStore,
    capability_store: CapabilityStore,
    handoff: InMemoryCredentialHandoff,
    presented_token_id: str,
) -> str | None:
    """Reproduces exactly the decision `executor._finish_reserve` makes on
    a matching reservation retry, against fresh (post-restart) store
    instances. Returns a fresh claim_id on successful recovery, or None if
    recovery is refused (wrong authorizer)."""
    outcome = core_store.reserve(
        "evt:reserve", "root", CHILD_DELEGATION_ID, "worker", Decimal("0.30")
    )
    assert outcome == "already_exists"
    authorization = capability_store.get_reservation_authorization(CHILD_DELEGATION_ID)
    assert authorization is not None
    if presented_token_id != authorization.authorizing_token_id:
        return None
    assert authorization.child_scopes == CHILD_SCOPES
    state = core_store.get(CHILD_DELEGATION_ID)
    assert state is not None and state.active
    assert not core_store.is_revocation_in_progress(CHILD_DELEGATION_ID)
    handoff.purge_for_delegations([CHILD_DELEGATION_ID])
    token_id, plaintext = capability_store.rotate_reservation_credential(
        CHILD_DELEGATION_ID, CHILD_SCOPES
    )
    return handoff.create(
        token_id,
        plaintext,
        "root",
        "reserve",
        authorizing_token_id=presented_token_id,
        child_delegation_id=CHILD_DELEGATION_ID,
    )


def _live_token_ids(cap_db_path: Path) -> list[str]:
    with sqlite3.connect(cap_db_path) as conn:
        rows = conn.execute(
            "SELECT token_id FROM capability_tokens WHERE delegation_id = ? AND revoked = 0",
            (CHILD_DELEGATION_ID,),
        ).fetchall()
        return [row[0] for row in rows]


def test_crash_at_every_boundary_lets_the_original_authorizer_recover_exactly_one_credential(
    tmp_path,
):
    for boundary in BOUNDARIES:
        db_path = tmp_path / f"{boundary}-authority.sqlite3"
        cap_db_path = tmp_path / f"{boundary}-capabilities.sqlite3"
        state_json_path = tmp_path / f"{boundary}-state.json"

        _run_helper(db_path, cap_db_path, state_json_path, boundary)
        state = json.loads(state_json_path.read_text())
        authorizer_token_id = state["authorizer_token_id"]
        authorizer_plaintext = state["authorizer_plaintext"]

        # A real restart: brand-new store instances and a brand-new,
        # empty in-memory handoff -- exactly as a freshly started server
        # process would have. Boundary (d) ("after server restart with
        # the original claim lost") is inherent here: no matter which
        # boundary the crash landed on, this fresh handoff has never seen
        # a claim.
        core_store = EconomicAuthorityStore(db_path)
        capability_store = CapabilityStore(cap_db_path)
        handoff = InMemoryCredentialHandoff()

        # Economic authority must never be reserved twice, at any
        # boundary -- true even before recovery is attempted, since
        # core.reserve() alone is what commits it.
        root_before = core_store.get("root")
        assert root_before is not None
        assert root_before.child_reserved_usd == Decimal("0.30")

        # A different credential -- one that never authorized this
        # reservation -- must not be able to recover, rotate, claim, or
        # mint access merely by knowing the child_delegation_id.
        _impostor_id, impostor_plaintext = capability_store.issue("root", {"reserve"})
        impostor_token_id = capability_store.token_id_for(impostor_plaintext)
        assert impostor_token_id is not None
        assert _recover(core_store, capability_store, handoff, impostor_token_id) is None, (
            f"[{boundary}] a different credential must not recover this reservation"
        )

        # The exact original authorizer recovers a fresh, usable claim.
        claim_id = _recover(core_store, capability_store, handoff, authorizer_token_id)
        assert claim_id is not None, f"[{boundary}] the original authorizer must recover"
        plaintext = handoff.redeem(capability_store, claim_id, authorizer_plaintext)
        info = capability_store.authorize(plaintext, CHILD_DELEGATION_ID, "read")
        assert info.delegation_id == CHILD_DELEGATION_ID

        # Exactly one child credential is live after recovery, regardless
        # of which boundary the crash hit.
        assert _live_token_ids(cap_db_path) == [
            capability_store.token_id_for(plaintext)
        ], f"[{boundary}] exactly one current child credential must work"

        # Economic authority is still not double-reserved after recovery.
        root_after = core_store.get("root")
        assert root_after is not None
        assert root_after.child_reserved_usd == Decimal("0.30"), (
            f"[{boundary}] economic authority must never be reserved twice"
        )

        # Plaintext never touches disk anywhere.
        assert authorizer_plaintext.encode("utf-8") not in db_path.read_bytes()
        assert authorizer_plaintext.encode("utf-8") not in cap_db_path.read_bytes()
        assert plaintext.encode("utf-8") not in cap_db_path.read_bytes()


def test_repeated_recovery_after_the_same_crash_keeps_exactly_one_live_credential(tmp_path):
    """Recovery itself must be safely repeatable: the original authorizer
    retrying several times after the same crash (e.g. its own retries
    racing a slow network) must never accumulate live credentials -- each
    recovery revokes the previous one."""
    db_path = tmp_path / "authority.sqlite3"
    cap_db_path = tmp_path / "capabilities.sqlite3"
    state_json_path = tmp_path / "state.json"

    _run_helper(db_path, cap_db_path, state_json_path, "after_claim_created")
    state = json.loads(state_json_path.read_text())
    authorizer_token_id = state["authorizer_token_id"]

    core_store = EconomicAuthorityStore(db_path)
    capability_store = CapabilityStore(cap_db_path)
    handoff = InMemoryCredentialHandoff()

    claim_ids = set()
    for _ in range(4):
        claim_id = _recover(core_store, capability_store, handoff, authorizer_token_id)
        assert claim_id is not None
        claim_ids.add(claim_id)

    assert len(claim_ids) == 4, "each recovery must mint a genuinely fresh claim"
    assert len(_live_token_ids(cap_db_path)) == 1

    root_state = core_store.get("root")
    assert root_state is not None
    assert root_state.child_reserved_usd == Decimal("0.30")
