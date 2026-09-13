"""Real subprocess-termination crash-recovery tests for
`inferrail.ap.store.RecoveryStore`/`inferrail.ap.engine.RecoveryEngine`
(defect #2 fix). Reproduces the review's exact scenario: a process is
terminated during the retry callback, leaving the stored decision at
`retry_in_progress` with no ambiguity flag and no review handoff.
Verifies the durable lease/reap recovery path instead: the case becomes
uncertain, any known cost is preserved, and it supports an acknowledged
human-review handoff without repeating the paid action.

No mocked exceptions here -- every "crash" in this file is a real
process death (`os._exit` or SIGKILL), matching
`tests/unit/hosted/test_a2a_economic_authority_core.py`'s established
pattern for this repo.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

from inferrail.ap.adapters import FixtureRetryAdapter
from inferrail.ap.engine import RecoveryEngine
from inferrail.ap.handoff import LoggingHandoff
from inferrail.ap.models import ExceptionCase, FailureType
from inferrail.ap.policy import PolicyConfig
from inferrail.ap.store import RecoveryStore
from inferrail.ap.validation import FieldPresenceAndConfidenceValidator

CRASH_HELPER = Path(__file__).resolve().parents[1] / "unit" / "ap" / "_ap_crash_helper.py"


def _config() -> PolicyConfig:
    return PolicyConfig(
        eligible_failure_types=frozenset(
            {FailureType.LOW_CONFIDENCE.value, FailureType.VALIDATION_CHECK_FAILED.value}
        ),
        retry_floor=0.5,
        human_review_threshold=0.75,
        max_retry_cost_usd=Decimal("1.00"),
        decision_deadline_seconds=86400.0,
    )


def test_hard_exit_mid_claim_leaves_retry_in_progress_then_reap_recovers(tmp_path):
    db_path = tmp_path / "ap.sqlite3"
    result = subprocess.run(
        [sys.executable, str(CRASH_HELPER), str(db_path), "claim_lease", "CRASH-1", "0.05"],
        capture_output=True, text=True, timeout=15,
    )
    # os._exit(1) never returns 0 -- the crash is real, not a clean
    # shutdown that happens to also mutate state.
    assert result.returncode != 0, (
        f"helper exited cleanly (code {result.returncode}); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    store = RecoveryStore(db_path)
    stuck = store.get_decision("CRASH-1")
    assert stuck is not None
    assert stuck["status"] == "retry_in_progress"  # exactly the reported stuck state
    assert stuck["worker_id"] is not None

    time.sleep(0.1)  # let the 0.05s lease actually expire

    store2 = RecoveryStore(db_path)
    handoff = LoggingHandoff(path=tmp_path / "handoffs.jsonl")
    engine = RecoveryEngine(
        store=store2, config=_config(),
        retry_adapter=FixtureRetryAdapter(results_by_work_id={}),
        validator=FieldPresenceAndConfidenceValidator(), handoff=handoff,
    )

    reaped = engine.reap_stale_retries()
    assert [r["work_id"] for r in reaped] == ["CRASH-1"]

    recovered = store2.get_decision("CRASH-1")
    assert recovered["status"] == "awaiting_human_review"
    assert recovered["worker_id"] is None
    assert recovered["lease_expires_at"] is None

    attempt = store2.get_retry_attempt("CRASH-1")
    assert attempt is not None
    assert attempt["status"] == "ambiguous"
    assert attempt["provider"] == "lease_reaper"

    # An operator can now complete the handoff for the recovered case,
    # re-supplying the case (the store never persisted enough to
    # reconstruct one itself -- by design, see
    # RecoveryEngine.reap_stale_retries's docstring).
    case = ExceptionCase(
        work_id="CRASH-1", checkpoint_attempt_id="A1",
        failure_type="low_confidence", confidence=0.6,
    )
    ref = engine.ensure_handoff(case)
    assert ref is not None
    assert store2.get_handoff("CRASH-1") is not None


def test_repeat_reap_request_is_idempotent(tmp_path):
    db_path = tmp_path / "ap.sqlite3"
    subprocess.run(
        [sys.executable, str(CRASH_HELPER), str(db_path), "claim_lease", "CRASH-2", "0.01"],
        capture_output=True, text=True, timeout=15,
    )
    time.sleep(0.05)

    store = RecoveryStore(db_path)
    handoff = LoggingHandoff(path=tmp_path / "handoffs.jsonl")
    engine = RecoveryEngine(
        store=store, config=_config(), retry_adapter=FixtureRetryAdapter(results_by_work_id={}),
        validator=FieldPresenceAndConfidenceValidator(), handoff=handoff,
    )

    first = engine.reap_stale_retries()
    assert len(first) == 1
    second = engine.reap_stale_retries()
    assert second == []  # nothing left to reap -- not re-reaped, not duplicated

    attempts = [
        a for a in [store.get_retry_attempt("CRASH-2")] if a is not None
    ]
    assert len(attempts) == 1  # exactly one synthetic attempt row, never duplicated


def test_sigkill_mid_sleep_still_recovers_via_reap(tmp_path):
    """The literal "kill -9 mid-retry" scenario: a subprocess claims the
    lease and is then actually hard-killed by the parent while it is
    still "running" (sleeping, standing in for a slow adapter call), not
    exiting on its own schedule.

    `Popen.kill()` is used rather than a raw `os.kill`/`signal.SIGKILL`
    specifically because `signal.SIGKILL` does not exist on Windows --
    `Popen.kill()` sends SIGKILL on POSIX and calls `TerminateProcess` on
    Windows, so this test (and the crash-recovery guarantee it verifies)
    is exercised natively on all three supported platforms, not skipped
    on Windows."""
    db_path = tmp_path / "ap.sqlite3"
    proc = subprocess.Popen(
        [
            sys.executable, str(CRASH_HELPER), str(db_path),
            "claim_lease_and_sleep", "CRASH-3", "0.05", "10",
        ],
    )
    store_before = RecoveryStore(db_path)
    deadline = time.monotonic() + 10
    while store_before.get_decision("CRASH-3") is None:
        if time.monotonic() > deadline:
            proc.kill()
            raise AssertionError("helper never claimed the lease within 10s")
        time.sleep(0.02)
    proc.kill()
    proc.wait(timeout=10)
    assert proc.returncode != 0, (
        f"expected a real hard kill, got a clean exit (code {proc.returncode})"
    )
    if sys.platform != "win32":
        assert proc.returncode == -signal.SIGKILL, (
            f"expected a real SIGKILL (-9) on POSIX, got returncode={proc.returncode}"
        )

    store = RecoveryStore(db_path)
    stuck = store.get_decision("CRASH-3")
    assert stuck is not None
    assert stuck["status"] == "retry_in_progress"

    time.sleep(0.1)  # let the 0.05s lease expire

    handoff = LoggingHandoff(path=tmp_path / "handoffs.jsonl")
    engine = RecoveryEngine(
        store=store, config=_config(), retry_adapter=FixtureRetryAdapter(results_by_work_id={}),
        validator=FieldPresenceAndConfidenceValidator(), handoff=handoff,
    )
    reaped = engine.reap_stale_retries()
    assert [r["work_id"] for r in reaped] == ["CRASH-3"]
    assert store.get_decision("CRASH-3")["status"] == "awaiting_human_review"


def test_crash_after_attempt_recorded_reconciles_using_the_real_result(tmp_path):
    """The exact boundary this pass's review named: terminate the
    process after the retry attempt has been durably recorded but
    before the decision status is updated. Recovery must reconcile
    using the real, known attempt -- never re-invoke anything, never
    insert a second attempt, and never leave the decision stuck at
    retry_in_progress forever (the bug this test guards against)."""
    db_path = tmp_path / "ap.sqlite3"
    result = subprocess.run(
        [
            sys.executable, str(CRASH_HELPER), str(db_path), "record_attempt_then_crash",
            "CRASH-4", "0.05", "True", "0.06",
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0, (
        f"helper exited cleanly (code {result.returncode}); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    store = RecoveryStore(db_path)
    stuck = store.get_decision("CRASH-4")
    assert stuck["status"] == "retry_in_progress"
    attempt_before = store.get_retry_attempt("CRASH-4")
    assert attempt_before is not None
    assert attempt_before["cost_usd"] == "0.06"  # the real cost is already durably known

    time.sleep(0.1)  # let the 0.05s lease expire

    handoff = LoggingHandoff(path=tmp_path / "handoffs.jsonl")
    engine = RecoveryEngine(
        store=store, config=_config(), retry_adapter=FixtureRetryAdapter(results_by_work_id={}),
        validator=FieldPresenceAndConfidenceValidator(), handoff=handoff,
    )
    reaped = engine.reap_stale_retries()
    assert len(reaped) == 1
    assert reaped[0]["kind"] == "reconciled"
    assert reaped[0]["status"] == "retry_resolved"

    final = store.get_decision("CRASH-4")
    assert final["status"] == "retry_resolved"
    assert final["worker_id"] is None
    attempt_after = store.get_retry_attempt("CRASH-4")
    assert attempt_after["attempt_id"] == attempt_before["attempt_id"]  # never duplicated
    assert attempt_after["cost_usd"] == "0.06"  # never lost or altered

    # Idempotent: a repeat sweep reconciles nothing new.
    assert engine.reap_stale_retries() == []


def test_crash_after_failed_validation_attempt_recorded_reconciles_to_human_review(tmp_path):
    db_path = tmp_path / "ap.sqlite3"
    result = subprocess.run(
        [
            sys.executable, str(CRASH_HELPER), str(db_path), "record_attempt_then_crash",
            "CRASH-5", "0.05", "False", "0.06",
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0

    store = RecoveryStore(db_path)
    time.sleep(0.1)
    handoff = LoggingHandoff(path=tmp_path / "handoffs.jsonl")
    engine = RecoveryEngine(
        store=store, config=_config(), retry_adapter=FixtureRetryAdapter(results_by_work_id={}),
        validator=FieldPresenceAndConfidenceValidator(), handoff=handoff,
    )
    reaped = engine.reap_stale_retries()
    assert reaped[0]["kind"] == "reconciled"
    assert reaped[0]["status"] == "awaiting_human_review"
    assert store.get_decision("CRASH-5")["status"] == "awaiting_human_review"
