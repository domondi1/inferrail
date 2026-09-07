"""Idempotency-store tests for hosted/work_economics/store.py. No network, no secrets."""

from __future__ import annotations

import sys
from pathlib import Path

HOSTED_DIR = Path(__file__).resolve().parents[3] / "hosted" / "work_economics"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

import pytest  # noqa: E402
from store import DurablePurchaseStore  # noqa: E402


@pytest.fixture
def store(tmp_path) -> DurablePurchaseStore:
    return DurablePurchaseStore(tmp_path / "purchases.sqlite3")


def test_create_quote_is_idempotent(store):
    first = store.create_quote("p1", "work-1", "0.05", "USD", "TEST_RAIL")
    second = store.create_quote("p1", "work-1", "0.05", "USD", "TEST_RAIL")
    assert first == second
    assert first["status"] == "QUOTED"


def test_record_payment_transitions_once(store):
    store.create_quote("p2", "work-2", "0.05", "USD", "TEST_RAIL")
    row1, newly_charged1 = store.record_payment("p2", "nonce-abc")
    row2, newly_charged2 = store.record_payment("p2", "nonce-abc")

    assert row1["status"] == "PAYMENT_VERIFIED"
    assert newly_charged1 is True
    assert newly_charged2 is False
    assert row2["payment_proof_ref"] == "nonce-abc"


def test_record_payment_without_quote_raises(store):
    with pytest.raises(KeyError):
        store.record_payment("no-such-purchase", "nonce")


def test_execute_once_runs_compute_fn_exactly_once(store):
    store.create_quote("p3", "work-3", "0.05", "USD", "TEST_RAIL")
    store.record_payment("p3", "nonce-xyz")

    calls = []

    def compute():
        calls.append(1)
        return '{"result": true}', '{"receipt": true}'

    row1, newly_executed1 = store.execute_once("p3", compute)
    row2, newly_executed2 = store.execute_once("p3", compute)

    assert len(calls) == 1  # not recomputed on the second call
    assert newly_executed1 is True
    assert newly_executed2 is False
    assert row1["result_json"] == row2["result_json"] == '{"result": true}'


def test_execute_once_before_payment_raises(store):
    store.create_quote("p4", "work-4", "0.05", "USD", "TEST_RAIL")
    with pytest.raises(PermissionError):
        store.execute_once("p4", lambda: ("{}", "{}"))


def test_get_returns_none_for_unknown_purchase(store):
    assert store.get("nonexistent") is None
