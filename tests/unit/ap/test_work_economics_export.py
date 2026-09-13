"""Tests for `work_economics_export.py` -- the smallest useful
connection between AP's own records and Work Economics' event contract.
Round-trips every exported dict through the real `EconomicEvent.
from_dict()` (imported the same way
`tests/unit/hosted/test_work_economics_capability.py` already reaches
`hosted/work_economics/`), so this is tested against the actual
contract, not a hand-guessed shape.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

from inferrail.ap.report import build_live_report
from inferrail.ap.store import RecoveryStore
from inferrail.ap.work_economics_export import (
    export_unconfirmed_late_result,
    export_work_economics_events,
)

HOSTED_DIR = Path(__file__).resolve().parents[3] / "hosted" / "work_economics"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

from capability import EconomicEvent  # noqa: E402


def _decision(store: RecoveryStore, work_id: str) -> None:
    store.create_decision(
        work_id=work_id, decision_id=f"dec-{work_id}", checkpoint_attempt_id="A1",
        failure_type="low_confidence", confidence="0.6", cost_so_far_usd="0.10",
        policy_name="candidate_policy", policy_version="ap.policy/v1",
        recommended_action="retry", reason="test", status="retry_in_progress",
    )


def test_retry_only_event_round_trips_through_economic_event_from_dict(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W1")
    store.record_retry_attempt(
        work_id="W1", attempt_id="ret-1", status="success", cost_usd="0.06",
        confidence="0.9", provider="fixture", validation_passed="True",
        validator_version="ap.validator/v1",
    )
    store.set_decision_status("W1", "retry_resolved")

    events = export_work_economics_events(store, "W1")
    assert len(events) == 1
    event = EconomicEvent.from_dict(events[0])
    assert event.resource_class == "ap_invoice_extraction_retry"
    assert event.supplier == "fixture"
    assert event.known_cost_usd == Decimal("0.06")
    assert event.price_basis == "PROVIDER_REPORTED_ESTIMATE"
    assert event.status == "success"


def test_retry_and_review_events_round_trip_and_sum_matches_observed_cost(
    tmp_path: Path,
) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W2")
    store.record_retry_attempt(
        work_id="W2", attempt_id="ret-2", status="failed", cost_usd="0.06",
        confidence="0.3", provider="fixture", validation_passed="False",
        validator_version="ap.validator/v1",
    )
    store.set_decision_status("W2", "awaiting_human_review")
    store.record_outcome(
        work_id="W2", outcome="corrected", timestamp=0.0, source="vendor_review_queue",
        correction_delta_usd="9.40", review_cost_usd="2.50",
    )

    events = export_work_economics_events(store, "W2")
    assert len(events) == 2
    economic_events = [EconomicEvent.from_dict(e) for e in events]
    assert {e.resource_class for e in economic_events} == {
        "ap_invoice_extraction_retry", "human_review",
    }
    exported_total = sum(e.known_cost_usd for e in economic_events if e.known_cost_usd is not None)

    row = build_live_report(store).rows[0]
    assert row.observed_cost_complete is True
    assert exported_total == row.observed_cost_usd


def test_unknown_review_cost_exports_unknown_price_basis_not_fabricated(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W3")
    store.set_decision_status("W3", "awaiting_human_review")
    store.record_outcome(
        work_id="W3", outcome="rejected", timestamp=0.0, source="vendor_review_queue",
        correction_delta_usd=None, review_cost_usd=None,
    )

    events = export_work_economics_events(store, "W3")
    review_events = [e for e in events if e["resource_class"] == "human_review"]
    assert len(review_events) == 1
    event = EconomicEvent.from_dict(review_events[0])
    assert event.price_basis == "UNKNOWN"
    assert event.known_cost_usd is None  # never fabricated, never dropped


def test_no_events_when_neither_attempt_nor_outcome_exist(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W4")
    assert export_work_economics_events(store, "W4") == []


def test_missing_work_id_raises_keyerror(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    with pytest.raises(KeyError):
        export_work_economics_events(store, "NEVER-DECIDED")


def test_late_result_is_never_included_in_the_summable_events_list(tmp_path: Path) -> None:
    """A late arrival must never silently become part of the total
    export_work_economics_events feeds into compute_work_economics."""
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W5")
    store.record_retry_attempt(
        work_id="W5", attempt_id="ret_reaped_W5", status="ambiguous",
        cost_usd=None, confidence=None, provider="lease_reaper",
    )
    store.set_decision_status("W5", "awaiting_human_review")
    store.record_late_retry_result(
        work_id="W5", attempt_id="ret-late", status="success",
        cost_usd="0.09", provider="fixture", detail="arrived after reap",
    )

    events = export_work_economics_events(store, "W5")
    assert len(events) == 1  # only the official ambiguous attempt
    official_event = EconomicEvent.from_dict(events[0])
    assert official_event.known_cost_usd is None  # the official record's cost stays unknown
    assert official_event.status == "error"


def test_export_unconfirmed_late_result_is_not_economic_event_shaped(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W6")
    store.record_retry_attempt(
        work_id="W6", attempt_id="ret_reaped_W6", status="ambiguous",
        cost_usd=None, confidence=None, provider="lease_reaper",
    )
    store.set_decision_status("W6", "awaiting_human_review")

    assert export_unconfirmed_late_result(store, "W6") is None

    store.record_late_retry_result(
        work_id="W6", attempt_id="ret-late", status="success",
        cost_usd="0.09", provider="fixture", detail="arrived after reap",
    )
    late = export_unconfirmed_late_result(store, "W6")
    assert late is not None
    assert late["confirmed"] is False
    assert late["known_cost_usd"] == "0.09"
    assert "price_basis" not in late  # deliberately not EconomicEvent-shaped
    with pytest.raises(Exception):  # noqa: B017, PT011 -- any failure proves the point
        EconomicEvent.from_dict(late)


def test_export_unconfirmed_late_result_surfaces_only_the_latest(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path / "ap.sqlite3")
    _decision(store, "W7")
    store.record_retry_attempt(
        work_id="W7", attempt_id="ret_reaped_W7", status="ambiguous",
        cost_usd=None, confidence=None, provider="lease_reaper",
    )
    store.set_decision_status("W7", "awaiting_human_review")
    store.record_late_retry_result(
        work_id="W7", attempt_id="ret-late-1", status="failed",
        cost_usd="0.05", provider="fixture", detail="first",
    )
    store.record_late_retry_result(
        work_id="W7", attempt_id="ret-late-2", status="success",
        cost_usd="0.09", provider="fixture", detail="second",
    )
    late = export_unconfirmed_late_result(store, "W7")
    assert late["attempt_id"] == "ret-late-2"
