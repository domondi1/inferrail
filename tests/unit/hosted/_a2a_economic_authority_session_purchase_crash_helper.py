"""Subprocess helper for crash-recovery testing of the Phase C
payment-to-session pipeline (`sessions.create_or_recover_session`'s three
durable-write steps: `record_session_purchase`, `core.create_root`,
`issue_or_rotate_session_credential`).

Not a test module itself (leading underscore keeps pytest from collecting
it). Performs exactly the steps up to (and including) the named boundary,
then hard-kills the process with `os._exit`, which skips atexit handlers,
`finally` blocks, and any buffered output -- proving durability and
recovery against a real, uncontrolled process death rather than a clean
shutdown, exactly like `_a2a_economic_authority_crash_helper.py` does for
core.py's own boundaries.

Usage: python3 _a2a_economic_authority_session_purchase_crash_helper.py
    <db_path> <capability_db_path> <payment_nonce> <recovery_secret_hash>
    <boundary>
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

_HOSTED_DIR = Path(__file__).resolve().parents[3] / "hosted" / "a2a_economic_authority"
if str(_HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(_HOSTED_DIR))

from capabilities import CapabilityStore  # noqa: E402
from core import EconomicAuthorityStore  # noqa: E402

_AGENT_ID = "crash-test-buyer"
_AUTHORITY_CEILING = "10.00"
_SERVICE_FEE = "0.05"
_SCOPES = frozenset({"read", "reserve", "grant", "consume", "settle", "revoke"})


def main() -> None:
    db_path, cap_db_path, payment_nonce, recovery_secret_hash, boundary = sys.argv[1:6]
    core = EconomicAuthorityStore(db_path)
    capabilities = CapabilityStore(cap_db_path)

    if boundary not in ("after_purchase_record", "after_root_created", "after_credential_issued"):
        raise ValueError(f"unknown boundary: {boundary}")

    # Standing in for x402's real "upfront" settlement, already completed
    # before this process (or `sessions.create_or_recover_session`) ever
    # runs -- see sessions.py's module docstring. This helper starts
    # AFTER settlement, at the first durable write this module performs.
    purchase = capabilities.record_session_purchase(
        payment_nonce, _AGENT_ID, _AUTHORITY_CEILING, _SERVICE_FEE,
        recovery_secret_hash=recovery_secret_hash,
    )
    if boundary == "after_purchase_record":
        os._exit(1)  # deliberate hard kill, see module docstring

    core.create_root(
        event_id=f"session-root:{payment_nonce}",
        delegation_id=purchase.session_id,
        agent_id=_AGENT_ID,
        envelope_usd=Decimal(_AUTHORITY_CEILING),
    )
    if boundary == "after_root_created":
        os._exit(1)

    capabilities.issue_or_rotate_session_credential(payment_nonce, purchase.session_id, _SCOPES)
    if boundary == "after_credential_issued":
        os._exit(1)


if __name__ == "__main__":
    main()
