"""Pure-logic tests for hosted/work_economics/capability.py. No network, no secrets."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

HOSTED_DIR = Path(__file__).resolve().parents[3] / "hosted" / "work_economics"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

import pytest  # noqa: E402
from capability import (  # noqa: E402
    CAPABILITY_VERSION,
    EconomicEvent,
    InvalidEconomicEvent,
    compute_work_economics,
)


def _event(**overrides) -> dict:
    base = {
        "resource_class": "inference",
        "supplier": "example-supplier",
        "known_cost_usd": "0.02",
        "price_basis": "LIST_PRICE_PUBLISHED",
        "currency": "USD",
        "status": "success",
    }
    base.update(overrides)
    return base


def test_single_known_event_totals_exactly():
    events = [EconomicEvent.from_dict(_event())]
    result = compute_work_economics("work-1", events, "success")

    assert result.known_total_cost_usd == Decimal("0.02")
    assert result.exact_total_known is True
    assert result.event_count == 1
    assert result.unknown_event_count == 0
    assert result.breakdown_by_resource_class == {"inference": Decimal("0.02")}
    assert result.breakdown_by_supplier == {"example-supplier": Decimal("0.02")}
    assert result.price_provenance == {"LIST_PRICE_PUBLISHED": 1}
    assert result.capability_version == CAPABILITY_VERSION


def test_unknown_event_never_fabricates_a_total():
    events = [
        EconomicEvent.from_dict(_event(known_cost_usd="0.02")),
        EconomicEvent.from_dict(
            _event(supplier="other", known_cost_usd=None, price_basis="UNKNOWN")
        ),
    ]
    result = compute_work_economics("work-2", events, None)

    # A known partial sum exists (0.02) but the total is not "exact" -- and
    # the caller must be able to tell those two things apart.
    assert result.known_total_cost_usd == Decimal("0.02")
    assert result.exact_total_known is False
    assert result.unknown_event_count == 1


def test_all_unknown_events_yields_null_total_not_zero():
    events = [EconomicEvent.from_dict(_event(known_cost_usd=None, price_basis="UNKNOWN"))]
    result = compute_work_economics("work-3", events, None)

    assert result.known_total_cost_usd is None
    assert result.exact_total_known is False
    assert result.unknown_event_count == 1


def test_breakdown_sums_multiple_events_per_key():
    events = [
        EconomicEvent.from_dict(
            _event(resource_class="inference", supplier="a", known_cost_usd="0.01")
        ),
        EconomicEvent.from_dict(
            _event(resource_class="inference", supplier="a", known_cost_usd="0.03")
        ),
        EconomicEvent.from_dict(
            _event(resource_class="search", supplier="b", known_cost_usd="0.05")
        ),
    ]
    result = compute_work_economics("work-4", events, None)

    assert result.known_total_cost_usd == Decimal("0.09")
    assert result.breakdown_by_resource_class == {
        "inference": Decimal("0.04"),
        "search": Decimal("0.05"),
    }
    assert result.breakdown_by_supplier == {"a": Decimal("0.04"), "b": Decimal("0.05")}


@pytest.mark.parametrize(
    "overrides",
    [
        {"resource_class": ""},
        {"supplier": ""},
        {"price_basis": "NOT_A_REAL_BASIS"},
        {"status": "NOT_A_REAL_STATUS"},
        {"currency": "EUR"},
        {"known_cost_usd": 0.02},  # float, not a decimal string
        {"known_cost_usd": "-0.01"},
    ],
)
def test_invalid_event_rejected(overrides):
    with pytest.raises(InvalidEconomicEvent):
        EconomicEvent.from_dict(_event(**overrides))


def test_unknown_basis_requires_null_cost():
    with pytest.raises(InvalidEconomicEvent):
        EconomicEvent.from_dict(_event(price_basis="UNKNOWN", known_cost_usd="0.01"))


def test_priced_basis_requires_non_null_cost():
    with pytest.raises(InvalidEconomicEvent):
        EconomicEvent.from_dict(_event(price_basis="LIST_PRICE_PUBLISHED", known_cost_usd=None))


def test_empty_events_list_rejected():
    with pytest.raises(InvalidEconomicEvent):
        compute_work_economics("work-5", [], None)


def test_missing_work_id_rejected():
    events = [EconomicEvent.from_dict(_event())]
    with pytest.raises(InvalidEconomicEvent):
        compute_work_economics("", events, None)


def test_result_json_dict_uses_decimal_strings_not_floats():
    events = [EconomicEvent.from_dict(_event())]
    result = compute_work_economics("work-6", events, "success")
    payload = result.to_json_dict()

    assert payload["known_total_cost_usd"] == "0.02"
    assert isinstance(payload["breakdown_by_resource_class"]["inference"], str)
