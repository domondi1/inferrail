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

    python3 _ap_crash_helper.py <db_path> record_attempt_then_crash <work_id> \
        <lease_seconds> <validation_passed: True|False> <cost_usd>
        Claims the lease, then durably records a real retry attempt
        (status="success", the given validation_passed and cost_usd) --
        exactly what `engine.RecoveryEngine._execute_retry` does right
        before it would call `set_decision_status`/`clear_retry_lease` --
        then hard-kills *before* that status transition ever runs. Proves
        the exact boundary the second crash-recovery defect names: the
        external action's result is already durably known, but the
        decision itself is still stuck at `retry_in_progress`.
"""

from __future__ import annotations

import os
import sys
import time

from inferrail.ap.store import RecoveryStore


def main() -> None:
    db_path, boundary, work_id = sys.argv[1], sys.argv[2], sys.argv[3]
    store = RecoveryStore(db_path)

    if boundary in ("claim_lease", "claim_lease_and_sleep", "record_attempt_then_crash"):
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

    if boundary == "record_attempt_then_crash":
        validation_passed, cost_usd = sys.argv[5], sys.argv[6]
        store.record_retry_attempt(
            work_id=work_id, attempt_id=f"ret-{work_id}", status="success",
            cost_usd=cost_usd, confidence="0.95", provider="crash_test_adapter",
            validation_passed=validation_passed, validator_version="ap.validator/v1",
        )
        # Deliberately hard-kill *before* set_decision_status/
        # clear_retry_lease -- the real attempt above is already
        # durably committed; only the decision's own status transition
        # never happens.

    os._exit(1)  # deliberate hard kill, see module docstring


if __name__ == "__main__":
    main()
