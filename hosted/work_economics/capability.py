"""Inferrail Work Economics: the sold capability.

Given a caller-declared list of economic events for one unit of AI work,
compute a normalized cost summary: a known total (only when every
contributing event has a known cost), a breakdown by resource class and by
supplier, a count of events whose cost is unknown, and where each cost
figure came from.

Pure and deterministic — no I/O, no network calls. This is the smallest
core the HTTP seller in `service.py` wraps.

Payload-freedom here is structural: `EconomicEvent` has no field capable of
holding prompt, response, or any other work-content payload, only cost
metadata about the event. This mirrors the same discipline already shipped
for `inferrail.receipts.InferenceReceipt` (Decimal-only money, an explicit
unknown-cost count rather than a fabricated total, a caller-declared
outcome echoed back but never interpreted) — see `docs/adr/0005`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal

PriceBasis = Literal["LIST_PRICE_PUBLISHED", "PROVIDER_REPORTED_ESTIMATE", "UNKNOWN"]
EventStatus = Literal["success", "error", "partial"]

VALID_PRICE_BASIS: set[str] = {"LIST_PRICE_PUBLISHED", "PROVIDER_REPORTED_ESTIMATE", "UNKNOWN"}
VALID_STATUS: set[str] = {"success", "error", "partial"}

CAPABILITY_NAME = "inferrail-work-economics"
CAPABILITY_VERSION = "work-economics-v1"


class InvalidEconomicEvent(ValueError):
    """Raised when a caller-supplied event fails validation."""


@dataclass(frozen=True)
class EconomicEvent:
    resource_class: str
    supplier: str
    price_basis: str
    status: str
    known_cost_usd: Decimal | None = None
    currency: str = "USD"

    @staticmethod
    def from_dict(raw: dict) -> EconomicEvent:
        if not isinstance(raw, dict):
            raise InvalidEconomicEvent("event must be an object")

        resource_class = raw.get("resource_class")
        supplier = raw.get("supplier")
        price_basis = raw.get("price_basis")
        status = raw.get("status")
        currency = raw.get("currency", "USD")

        if not resource_class or not isinstance(resource_class, str):
            raise InvalidEconomicEvent("resource_class is required")
        if not supplier or not isinstance(supplier, str):
            raise InvalidEconomicEvent("supplier is required")
        if price_basis not in VALID_PRICE_BASIS:
            raise InvalidEconomicEvent(f"price_basis must be one of {sorted(VALID_PRICE_BASIS)}")
        if status not in VALID_STATUS:
            raise InvalidEconomicEvent(f"status must be one of {sorted(VALID_STATUS)}")
        if currency != "USD":
            raise InvalidEconomicEvent("only currency=USD is supported in v1")

        raw_cost = raw.get("known_cost_usd")
        cost: Decimal | None = None
        if raw_cost is not None:
            if not isinstance(raw_cost, str):
                raise InvalidEconomicEvent(
                    "known_cost_usd must be a decimal string or null, never a float"
                )
            try:
                cost = Decimal(raw_cost)
            except InvalidOperation as exc:
                raise InvalidEconomicEvent("known_cost_usd is not a valid decimal string") from exc
            if cost < 0:
                raise InvalidEconomicEvent("known_cost_usd cannot be negative")

        if price_basis == "UNKNOWN" and cost is not None:
            raise InvalidEconomicEvent("price_basis=UNKNOWN requires known_cost_usd=null")
        if price_basis != "UNKNOWN" and cost is None:
            raise InvalidEconomicEvent("a priced basis requires a non-null known_cost_usd")

        return EconomicEvent(
            resource_class=resource_class,
            supplier=supplier,
            price_basis=price_basis,
            status=status,
            known_cost_usd=cost,
            currency=currency,
        )


@dataclass(frozen=True)
class WorkEconomicsResult:
    work_id: str
    known_total_cost_usd: Decimal | None
    exact_total_known: bool
    event_count: int
    unknown_event_count: int
    breakdown_by_resource_class: dict[str, Decimal] = field(default_factory=dict)
    breakdown_by_supplier: dict[str, Decimal] = field(default_factory=dict)
    price_provenance: dict[str, int] = field(default_factory=dict)
    outcome_status: str | None = None
    capability_version: str = CAPABILITY_VERSION

    def to_json_dict(self) -> dict:
        return {
            "work_id": self.work_id,
            "known_total_cost_usd": (
                str(self.known_total_cost_usd) if self.known_total_cost_usd is not None else None
            ),
            "exact_total_known": self.exact_total_known,
            "event_count": self.event_count,
            "unknown_event_count": self.unknown_event_count,
            "breakdown_by_resource_class": {
                k: str(v) for k, v in sorted(self.breakdown_by_resource_class.items())
            },
            "breakdown_by_supplier": {
                k: str(v) for k, v in sorted(self.breakdown_by_supplier.items())
            },
            "price_provenance": dict(sorted(self.price_provenance.items())),
            "outcome_status": self.outcome_status,
            "capability_version": self.capability_version,
        }


def compute_work_economics(
    work_id: str,
    events: list[EconomicEvent],
    outcome_status: str | None,
) -> WorkEconomicsResult:
    """Pure aggregation. Never fabricates a total across unknown-cost events."""
    if not work_id or not isinstance(work_id, str):
        raise InvalidEconomicEvent("work_id is required")
    if not events:
        raise InvalidEconomicEvent("at least one economic event is required")

    known_total = Decimal("0")
    have_any_known = False
    unknown_count = 0
    by_resource: dict[str, Decimal] = {}
    by_supplier: dict[str, Decimal] = {}
    provenance: dict[str, int] = {}

    for event in events:
        provenance[event.price_basis] = provenance.get(event.price_basis, 0) + 1
        if event.known_cost_usd is None:
            unknown_count += 1
            continue
        have_any_known = True
        known_total += event.known_cost_usd
        by_resource[event.resource_class] = (
            by_resource.get(event.resource_class, Decimal("0")) + event.known_cost_usd
        )
        by_supplier[event.supplier] = (
            by_supplier.get(event.supplier, Decimal("0")) + event.known_cost_usd
        )

    return WorkEconomicsResult(
        work_id=work_id,
        known_total_cost_usd=known_total if have_any_known else None,
        exact_total_known=unknown_count == 0,
        event_count=len(events),
        unknown_event_count=unknown_count,
        breakdown_by_resource_class=by_resource,
        breakdown_by_supplier=by_supplier,
        price_provenance=provenance,
        outcome_status=outcome_status,
    )
