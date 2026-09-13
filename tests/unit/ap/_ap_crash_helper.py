"""Subprocess helper for crash-recovery testing of `RecoveryStore`/
`RecoveryEngine`. Not a test module itself (leading underscore keeps
pytest from collecting it) -- mirrors
`tests/unit/hosted/_a2a_economic_authority_crash_helper.py`'s exact
pattern: perform exactly one committed mutation, then hard-kill the
process with `os._exit`, which skips atexit handlers, `finally` blocks,
and buffered output -- proving durability against a real, uncontrolled
process death rather than a clean shutdown.

Usage:
    python3 _ap_crash_helper.py <db_path> claim_lease <work_id> <lease_seconds>
        Claims a retry_in_progress decision (with a worker_id/lease) for
        <work_id>, exactly like `RecoveryEngine.decide` does right before
        invoking the retry adapter, then hard-kills -- simulating a
        process that died before the adapter call even returned.

    python3 _ap_crash_helper.py <db_path> claim_lease_and_sleep <work_id> \
        <lease_seconds> <sleep_seconds>
        Same, but sleeps for <sleep_seconds> afterward instead of exiting
        immediately -- for a parent test to SIGKILL it externally while
        it is "mid-retry", rather than the helper choosing its own exit.
"""

from __future__ import annotations

import os
import sys
import time

from inferrail.ap.store import RecoveryStore


def main() -> None:
    db_path, boundary, work_id = sys.argv[1], sys.argv[2], sys.argv[3]
    store = RecoveryStore(db_path)

    if boundary in ("claim_lease", "claim_lease_and_sleep"):
        lease_seconds = float(sys.argv[4])
        store.create_decision(
            work_id=work_id, decision_id=f"dec-{work_id}", checkpoint_attempt_id="A1",
            failure_type="low_confidence", confidence="0.6", cost_so_far_usd="0.10",
            policy_name="candidate_policy", policy_version="ap.policy/v1",
            recommended_action="retry", reason="crash-test",
            status="retry_in_progress",
            worker_id=f"crashed-worker-{os.getpid()}",
            lease_expires_at=time.time() + lease_seconds,
        )
    else:
        raise ValueError(f"unknown boundary: {boundary}")

    if boundary == "claim_lease_and_sleep":
        time.sleep(float(sys.argv[5]))
        # If we get here, the parent didn't kill us in time -- exit
        # cleanly so the test can tell the difference.
        sys.exit(0)

    os._exit(1)  # deliberate hard kill, see module docstring


if __name__ == "__main__":
    main()
