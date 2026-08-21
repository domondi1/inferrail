from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from inferrail.receipts.schema import InferenceReceipt
from inferrail.transactions.builder import build_transaction, new_transaction_id
from inferrail.transactions.schema import EconomicEventRef, TaskTransaction

_T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _receipt(**overrides: object) -> InferenceReceipt:
    defaults: dict[str, object] = dict(
        receipt_id="ir_x",
        request_id="req_x",
        route="default",
        provider="openai",
        model="gpt-4o-mini",
        status="success",
        total_latency_ms=10.0,
        timestamp=_T0,
    )
    defaults.update(overrides)
    return InferenceReceipt(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Schema: privacy structure + provenance
# ---------------------------------------------------------------------------


def test_task_transaction_has_no_payload_fields() -> None:
    field_names = set(TaskTransaction.model_fields) | set(EconomicEventRef.model_fields)
    assert field_names.isdisjoint({"prompt", "messages", "content", "response", "arguments"})


def test_task_transaction_json_serializes_cost_as_decimal_string_not_float() -> None:
    tx = TaskTransaction(
        transaction_id="tx_test",
        task_id="bug_9281",
        events=[
            EconomicEventRef(
                event_type="inference", event_id="ir_1", cost_usd=Decimal("0.45"), status="success"
            )
        ],
        known_total_cost_usd=Decimal("0.45"),
        unknown_cost_event_count=0,
        status="success",
        started_at=_T0,
        ended_at=_T0,
    )

    raw = json.loads(tx.model_dump_json())

    assert isinstance(raw["known_total_cost_usd"], str)
    assert isinstance(raw["events"][0]["cost_usd"], str)


def test_new_transaction_id_is_unique() -> None:
    assert new_transaction_id() != new_transaction_id()


# ---------------------------------------------------------------------------
# build_transaction: filtering, aggregation, status rule
# ---------------------------------------------------------------------------


def test_build_transaction_filters_by_attribute_value() -> None:
    receipts = [
        _receipt(receipt_id="ir_1", attributes={"task_id": "bug_9281"}),
        _receipt(receipt_id="ir_2", attributes={"task_id": "other_task"}),
    ]

    tx = build_transaction("bug_9281", receipts)

    assert tx is not None
    assert [e.event_id for e in tx.events] == ["ir_1"]


def test_build_transaction_no_match_returns_none() -> None:
    receipts = [_receipt(attributes={"task_id": "other_task"})]

    assert build_transaction("bug_9281", receipts) is None


def test_build_transaction_custom_attribute_name() -> None:
    receipts = [_receipt(receipt_id="ir_1", attributes={"run_id": "run_42"})]

    tx = build_transaction("run_42", receipts, attribute_name="run_id")

    assert tx is not None
    assert tx.task_id == "run_42"


def test_build_transaction_sums_known_cost_exactly() -> None:
    receipts = [
        _receipt(
            receipt_id="ir_1",
            attributes={"task_id": "t"},
            estimated_cost_usd=Decimal("0.450000"),
        ),
        _receipt(
            receipt_id="ir_2",
            attributes={"task_id": "t"},
            estimated_cost_usd=Decimal("0.070000"),
        ),
    ]

    tx = build_transaction("t", receipts)

    assert tx is not None
    assert tx.known_total_cost_usd == Decimal("0.520000")


def test_build_transaction_unknown_cost_counted_only_for_successful_events() -> None:
    receipts = [
        # Successful, unresolvable pricing: a real pricing gap.
        _receipt(receipt_id="ir_1", attributes={"task_id": "t"}, status="success"),
        # Failed: no usage occurred, not a pricing gap.
        _receipt(receipt_id="ir_2", attributes={"task_id": "t"}, status="error"),
    ]

    tx = build_transaction("t", receipts)

    assert tx is not None
    assert tx.unknown_cost_event_count == 1
    assert tx.known_total_cost_usd == Decimal("0")


def test_build_transaction_never_shows_fabricated_zero_and_status_is_error() -> None:
    receipts = [_receipt(attributes={"task_id": "t"}, status="error")]

    tx = build_transaction("t", receipts)

    assert tx is not None
    assert tx.status == "error"
    assert tx.known_total_cost_usd == Decimal("0")
    assert tx.unknown_cost_event_count == 0  # error receipts aren't a pricing gap


def test_build_transaction_status_all_success() -> None:
    receipts = [
        _receipt(receipt_id="ir_1", attributes={"task_id": "t"}, status="success"),
        _receipt(receipt_id="ir_2", attributes={"task_id": "t"}, status="success"),
    ]

    tx = build_transaction("t", receipts)

    assert tx is not None
    assert tx.status == "success"


def test_build_transaction_status_all_error() -> None:
    receipts = [
        _receipt(receipt_id="ir_1", attributes={"task_id": "t"}, status="error"),
        _receipt(receipt_id="ir_2", attributes={"task_id": "t"}, status="error"),
    ]

    tx = build_transaction("t", receipts)

    assert tx is not None
    assert tx.status == "error"


def test_build_transaction_status_mixed_is_partial() -> None:
    receipts = [
        _receipt(receipt_id="ir_1", attributes={"task_id": "t"}, status="success"),
        _receipt(receipt_id="ir_2", attributes={"task_id": "t"}, status="error"),
    ]

    tx = build_transaction("t", receipts)

    assert tx is not None
    assert tx.status == "partial"


def test_build_transaction_any_partial_event_makes_transaction_partial() -> None:
    receipts = [
        _receipt(receipt_id="ir_1", attributes={"task_id": "t"}, status="success"),
        _receipt(receipt_id="ir_2", attributes={"task_id": "t"}, status="partial"),
    ]

    tx = build_transaction("t", receipts)

    assert tx is not None
    assert tx.status == "partial"


def test_build_transaction_started_and_ended_span_all_matching_receipts() -> None:
    t1 = _T0
    t2 = _T0 + timedelta(seconds=30)
    t3 = _T0 + timedelta(seconds=90)
    receipts = [
        _receipt(receipt_id="ir_2", attributes={"task_id": "t"}, timestamp=t2),
        _receipt(receipt_id="ir_1", attributes={"task_id": "t"}, timestamp=t1),
        _receipt(receipt_id="ir_3", attributes={"task_id": "t"}, timestamp=t3),
    ]

    tx = build_transaction("t", receipts)

    assert tx is not None
    assert tx.started_at == t1
    assert tx.ended_at == t3
    # Events are ordered chronologically, not input order.
    assert [e.event_id for e in tx.events] == ["ir_1", "ir_2", "ir_3"]


def test_build_transaction_event_type_is_always_inference_today() -> None:
    receipts = [_receipt(attributes={"task_id": "t"})]

    tx = build_transaction("t", receipts)

    assert tx is not None
    assert all(e.event_type == "inference" for e in tx.events)
