"""Per-attempt budget admission and settlement, shared by both gateway
engines (`InferenceEngine` for `/v1/chat/completions`,
`AnthropicInferenceEngine` for `/v1/messages`) — the same way they share
one `BudgetEnforcer` (ADR-0015). See
docs/adr/0021-atomic-budget-reservations.md.

Each provider attempt is admitted on its own (`_admit`): a reservation
is taken atomically before the provider is contacted, or the attempt is
refused. When the attempt ends it is settled: `_settle_failed_attempt`
for a failure with no usage, `_settle_and_emit_receipt` for an attempt
that reached the provider and produced a receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from inferrail.budgets.enforcement import (
    BudgetEnforcer,
    attempt_may_have_billed,
    augment_attributes_with_block,
    augment_attributes_with_held,
    settle_as_hold,
)
from inferrail.budgets.store import Reservation
from inferrail.errors import BudgetExceededError, BudgetUnpricedModelError, InferrailError
from inferrail.receipts.schema import InferenceReceipt
from inferrail.receipts.sinks import ReceiptSink
from inferrail.routing.router import RoutingDecision


@dataclass
class BudgetState:
    """Per-request budget bookkeeping across provider attempts: the
    reservation-estimate inputs, and the total left held by earlier
    attempts (surfaced as `budget_held_usd` on the request's receipt)."""

    prompt_chars: int
    max_completion_tokens: int
    held_usd: Decimal = field(default_factory=Decimal)


class BudgetAdmission:
    """Mixin for the two engines. Both already carry `_budgets` and
    `_receipts` and implement `_emit_failure` with the same signature."""

    _budgets: BudgetEnforcer | None
    _receipts: ReceiptSink

    def _emit_failure(
        self,
        request_id: str,
        route: str,
        provider: str,
        model: str,
        retry_count: int,
        started: float,
        exc: InferrailError,
        attributes: dict[str, str],
    ) -> None:
        raise NotImplementedError

    def _admit(
        self,
        request_id: str,
        decision: RoutingDecision,
        started: float,
        attributes: dict[str, str],
        attempt: int,
        budget: BudgetState,
    ) -> Reservation | None:
        """Pre-flight admission for one provider attempt — reserves
        atomically or raises `BudgetExceededError` /
        `BudgetUnpricedModelError` (both `InferrailError`s, caught by
        `gateway/app.py` like any other) before the provider is contacted.
        A no-op when no `BudgetEnforcer` is wired (the default — see
        `InferrailConfig.budgets.enabled`). Called once per attempt, so a
        retry is admitted (and can be refused) on its own.

        A refusal is recorded through the same `_emit_failure` path as any
        other pre-execution rejection — MISSION.md's acceptance criterion
        is not just "blocked before the provider is called" but "the
        block is visible in the store", so this must never fail silently.
        """
        if self._budgets is None:
            return None
        try:
            return self._budgets.reserve(
                request_id=request_id,
                provider=decision.provider_name,
                model=decision.model,
                attributes=attributes,
                prompt_chars=budget.prompt_chars,
                max_completion_tokens=budget.max_completion_tokens,
            )
        except (BudgetExceededError, BudgetUnpricedModelError) as exc:
            self._emit_failure(
                request_id, decision.route_name, decision.provider_name,
                decision.model, attempt, started, exc,
                augment_attributes_with_held(
                    augment_attributes_with_block(attributes, exc), budget.held_usd
                ),
            )
            raise

    def _settle_failed_attempt(
        self, reservation: Reservation | None, exc: BaseException, budget: BudgetState
    ) -> None:
        """A failed attempt produced no usage: release unless the provider
        may have billed it — see `budgets.enforcement.attempt_may_have_billed`."""
        if self._budgets is None or reservation is None:
            return
        if attempt_may_have_billed(exc):
            self._budgets.hold(reservation)
            budget.held_usd += reservation.amount_usd
        else:
            self._budgets.release(reservation)

    def _settle_and_emit_receipt(
        self, receipt: InferenceReceipt, reservation: Reservation | None, budget: BudgetState
    ) -> None:
        """Emit the receipt of an attempt that reached the provider, and
        settle its reservation: hold when there's no priced cost, else
        release — strictly *after* the receipt is written, so its cost is
        already counted as committed spend (briefly double-counted, never
        uncounted)."""
        hold = reservation is not None and settle_as_hold(
            cost_known=receipt.estimated_cost_usd is not None, may_have_billed=True
        )
        if hold:
            assert reservation is not None and self._budgets is not None
            self._budgets.hold(reservation)
            budget.held_usd += reservation.amount_usd
        if budget.held_usd:
            receipt = receipt.model_copy(
                update={
                    "attributes": augment_attributes_with_held(
                        receipt.attributes, budget.held_usd
                    )
                }
            )
        self._receipts.emit(receipt)
        if reservation is not None and not hold:
            assert self._budgets is not None
            self._budgets.release(reservation)
