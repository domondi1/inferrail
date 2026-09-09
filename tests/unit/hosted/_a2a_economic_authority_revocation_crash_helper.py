"""Subprocess helper for crash-injection testing of tree revocation.

Not a test module itself (leading underscore keeps pytest from collecting
it). Builds a small delegation tree (root -> child -> grandchild) with
issued capabilities, then durably marks `root` as undergoing revocation
via `core.mark_revocation_started` -- the FIRST step any tree teardown
takes -- and hard-kills the process with `os._exit` immediately after,
before any of the settlement/token-revocation/claim-purge work that would
normally follow in the same call. This proves the mark survives a crash
at exactly the boundary repair item 3 is about, and that a caller who
retries the revoke afterward safely resumes and finishes it.

Usage: python3 _a2a_economic_authority_revocation_crash_helper.py
           <db_path> <capability_db_path> <state_json_path>
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

from capabilities import CapabilityStore  # noqa: E402
from core import EconomicAuthorityStore  # noqa: E402


def main() -> None:
    db_path, capability_db_path, state_json_path = sys.argv[1], sys.argv[2], sys.argv[3]

    core_store = EconomicAuthorityStore(db_path)
    capability_store = CapabilityStore(capability_db_path)

    core_store.create_root("evt:root", "root", "buyer", Decimal("1.00"))
    core_store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.50"))
    core_store.reserve("evt:r2", "child-1", "grandchild-1", "sub-worker", Decimal("0.20"))

    child_token_id, child_plaintext = capability_store.issue(
        "child-1", {"read", "consume", "settle"}
    )
    grandchild_token_id, grandchild_plaintext = capability_store.issue(
        "grandchild-1", {"read", "consume", "settle"}
    )

    # Record the plaintext tokens for the test to use -- this file exists
    # only for whitebox test scaffolding and is never part of the real
    # service (the real service never persists a plaintext token anywhere).
    Path(state_json_path).write_text(
        json.dumps(
            {
                "child_token_id": child_token_id,
                "child_plaintext": child_plaintext,
                "grandchild_token_id": grandchild_token_id,
                "grandchild_plaintext": grandchild_plaintext,
            }
        )
    )

    # The crash boundary: mark committed, nothing else has happened yet.
    core_store.mark_revocation_started("root")

    os._exit(1)  # deliberate hard kill, see module docstring


if __name__ == "__main__":
    main()
