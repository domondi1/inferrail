"""Subprocess helper for crash-injection testing of reservation-credential
recovery (finding 1: crash-safe child credential recovery).

Not a test module itself (leading underscore keeps pytest from collecting
it). Performs the exact same sequence
`executor._finish_reserve`/`_mint_reservation_credential` perform when
minting a brand-new child credential for a reservation -- against real,
separate core/capability SQLite databases, exactly like the real service
uses -- then hard-kills the process with `os._exit` at one of three
boundaries, before whatever would normally happen next. `os._exit` skips
atexit handlers, `finally` blocks, and buffered output, so this proves
durability against a real, uncontrolled process death, not a clean
shutdown that happens to also mutate state.

The caller (a test in the same process, after this subprocess has died)
reopens fresh store instances against the same database files -- a real
restart -- and drives the recovery logic itself to prove: the exact
original authorizer can recover a fresh, usable child credential; a
different credential cannot; and exactly one child credential ends up
live no matter which boundary the crash landed on.

Usage: python3 _a2a_economic_authority_reserve_recovery_crash_helper.py
           <db_path> <capability_db_path> <state_json_path> <boundary>

<boundary> is one of:
    after_economic_reserve     -- (a) reservation committed, nothing else
    after_capability_issuance  -- (b) child token minted, no claim yet
    after_claim_created        -- (c)/(d) claim created, never delivered
                                   or read before this process (a stand-in
                                   for the real server) goes away
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

_HOSTED_DIR = Path(__file__).resolve().parents[3] / "hosted" / "a2a_economic_authority"
if str(_HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(_HOSTED_DIR))

from capabilities import CapabilityStore, InMemoryCredentialHandoff  # noqa: E402
from core import EconomicAuthorityStore  # noqa: E402

CHILD_DELEGATION_ID = "child-recovery-crash"
CHILD_SCOPES = frozenset({"read", "consume", "settle"})


def main() -> None:
    db_path, capability_db_path, state_json_path, boundary = sys.argv[1:5]

    core_store = EconomicAuthorityStore(db_path)
    capability_store = CapabilityStore(capability_db_path)

    core_store.create_root("bootstrap:root", "root", "root-owner", Decimal("1.00"))
    authorizer_token_id, authorizer_plaintext = capability_store.issue("root", {"reserve"})

    # Recorded for the test to use -- this file exists only for whitebox
    # test scaffolding and is never part of the real service (the real
    # service never persists a plaintext token anywhere).
    Path(state_json_path).write_text(
        json.dumps(
            {
                "authorizer_token_id": authorizer_token_id,
                "authorizer_plaintext": authorizer_plaintext,
            }
        )
    )

    # Step 1, durable, in capabilities.sqlite3, BEFORE the economic
    # reservation is ever committed -- see
    # `capabilities.record_reservation_authorization`.
    capability_store.record_reservation_authorization(
        CHILD_DELEGATION_ID, authorizer_token_id, CHILD_SCOPES
    )

    # Step 2, durable, in authority.sqlite3: the economic reservation
    # itself.
    outcome = core_store.reserve(
        "evt:reserve", "root", CHILD_DELEGATION_ID, "worker", Decimal("0.30")
    )
    assert outcome == "created"

    if boundary == "after_economic_reserve":
        os._exit(1)  # deliberate hard kill, see module docstring

    # Step 3, durable, in capabilities.sqlite3: mint the child credential.
    # On this very first mint there is nothing live yet to revoke.
    token_id, plaintext = capability_store.rotate_reservation_credential(
        CHILD_DELEGATION_ID, CHILD_SCOPES
    )

    if boundary == "after_capability_issuance":
        os._exit(1)  # deliberate hard kill, see module docstring

    # Step 4, EPHEMERAL, process-local only: the claim. This process is
    # about to die, so -- exactly like a real crash, or a real response
    # lost in transit -- this claim never reaches whoever would redeem it.
    handoff = InMemoryCredentialHandoff()
    handoff.create(
        token_id,
        plaintext,
        "root",
        "reserve",
        authorizing_token_id=authorizer_token_id,
        child_delegation_id=CHILD_DELEGATION_ID,
    )

    if boundary == "after_claim_created":
        os._exit(1)  # deliberate hard kill, see module docstring

    raise ValueError(f"unknown boundary: {boundary!r}")


if __name__ == "__main__":
    main()
