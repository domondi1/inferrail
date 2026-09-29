"""Pre-flight budget admission (with atomic reservations) and post-flight
overrun reconciliation.

Pre-flight (`BudgetEnforcer.reserve`, called from the gateway before
each provider attempt — see `gateway/execution.py` and
`gateway/anthropic_execution.py`): for every budget whose scope matches
the request's attribution (global always matches; project/work_id match
on `attributes["project"]`/`attributes["work_id"]`), compute
spent-so-far (from `ReceiptsStore.query()`, honest — only ever sums
receipts with a real, priced cost) plus outstanding reservations of
other in-flight or held attempts, plus a catalog-based conservative
estimate of this request's own cost. If a "block"-mode budget would be
exceeded, raise `BudgetExceededError` before the provider is ever called
— see docs/adr/0015-budget-enforcement.md. Otherwise the estimate is
reserved, all inside one SQLite write transaction, so concurrent
requests can't each spend the same remaining dollars — see
docs/adr/0021-atomic-budget-reservations.md. A "warn"-mode budget that
would be exceeded never raises; it's surfaced only via
`budget_overrun_usd` after the fact (see below).

Settlement (`release` / `hold`, called by the engines once an attempt
ends): released when the attempt's cost is known (after its receipt is
written) or the provider definitely rejected it; held — still counted,
never shown as cost — when the provider may have billed but no cost can
be computed. `settle_as_hold` is that rule.

Post-flight (`augment_attributes_with_overrun`, called once actual usage
is known, right before the receipt is emitted): recomputes spend using
the *actual* cost of this request and, if that pushes any matching
budget over its limit, adds a `budget_overrun_usd` attribute to the
receipt. This is the only place an overrun is ever recorded — never
fabricated at pre-flight time, since the pre-flight estimate is
deliberately conservative (an upper bound, not the real cost) and a
"block"-mode budget that let the request through by definition didn't
project an overrun.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from inferrail.budgets.schema import Budget, new_budget_id
from inferrail.budgets.store import AdmissionTransaction, BudgetStore, Reservation
from inferrail.errors import (
    AuthenticationError,
    BudgetDeclarationError,
    BudgetExceededError,
    BudgetUnpricedModelError,
    InferrailError,
    ProviderError,
    ProviderTimeoutError,
)
from inferrail.pricing.resolver import PricingResolver
from inferrail.receipts.calculator import CacheTokens, calculate_cost_with_cache_usd
from inferrail.receipts.sqlite_store import ReceiptsStore

_logger = logging.getLogger("inferrail.budgets")

_MILLION = Decimal(1_000_000)
_QUANTUM = Decimal("0.000001")

# Deliberately conservative: real English-text tokenizers average roughly
# 4 characters per token, so dividing by 3 overestimates prompt tokens —
# an upper bound, never an attempt at a real count (Inferrail has no
# tokenizer dependency, by design; see docs/adr/0015).
_CHARS_PER_TOKEN_UPPER_BOUND = 3

# Used only when a request doesn't specify max_tokens (OpenAI allows
# omitting it; Anthropic's Messages API requires it, so this constant is
# never used on that path). A generous, explicit assumption, not a
# fabricated exact count — see docs/adr/0015's "Known limitations".
DEFAULT_MAX_COMPLETION_TOKENS_ESTIMATE = 4096


def approx_char_count(value: object) -> int:
    """Sums string lengths anywhere inside a JSON-like structure (dict,
    list, or str) — used to turn a request's messages/system content into
    a prompt-size upper bound without needing a real tokenizer."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, Mapping):
        return sum(approx_char_count(v) for v in value.values())
    if isinstance(value, list | tuple):
        return sum(approx_char_count(v) for v in value)
    return 0


def estimate_upper_bound_usd(
    *,
    prompt_chars: int,
    max_completion_tokens: int,
    provider: str,
    model: str,
    pricing_resolver: PricingResolver,
) -> Decimal | None:
    """A catalog-based upper bound on this request's own cost. `None` —
    never a fabricated number — when the (provider, model) pair has no
    verified price, matching `receipts.builder.build_receipt`'s own
    honesty rule."""
    price = pricing_resolver.resolve(provider, model)
    if price is None:
        return None
    # Ceiling division: rounding the token estimate down would make this
    # not actually an upper bound.
    prompt_tokens_estimate = -(-prompt_chars // _CHARS_PER_TOKEN_UPPER_BOUND)
    input_cost = Decimal(prompt_tokens_estimate) * price.input_usd_per_million / _MILLION
    output_cost = Decimal(max_completion_tokens) * price.output_usd_per_million / _MILLION
    return (input_cost + output_cost).quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def matching_budgets(budgets: Iterable[Budget], attributes: Mapping[str, str]) -> list[Budget]:
    """Every budget whose scope applies to this request's attribution,
    in a stable, deterministic order (by `budget_id`) so enforcement
    order — and which budget's error surfaces first when several would
    block — is reproducible in tests and logs."""
    matched = []
    for budget in budgets:
        if budget.scope == "global":
            matched.append(budget)
        elif budget.scope == "project" and attributes.get("project") == budget.scope_value:
            matched.append(budget)
        elif budget.scope == "work_id" and attributes.get("work_id") == budget.scope_value:
            matched.append(budget)
    return sorted(matched, key=lambda b: b.budget_id)


def _window_start(window: str) -> datetime | None:
    now = datetime.now(UTC)
    if window == "daily":
        return datetime(now.year, now.month, now.day, tzinfo=UTC)
    if window == "monthly":
        return datetime(now.year, now.month, 1, tzinfo=UTC)
    # "per_work": no time boundary — the scope (a single work_id) already
    # bounds it to that unit of work's whole lifetime.
    return None


@dataclass(frozen=True)
class SpentSoFar:
    spent_usd: Decimal
    has_unpriced_usage: bool


def spent_so_far_usd(receipts: ReceiptsStore, budget: Budget) -> SpentSoFar:
    """Sums known, priced cost for every receipt this budget's scope
    covers, within its window. Only `status in ("success", "partial")`
    receipts are considered — an "error" receipt never billed any tokens.
    A receipt with real usage but no known price (an unrecognized model)
    is never silently treated as $0 — it's counted in
    `has_unpriced_usage` instead, so callers can stay honest about the
    gap rather than under-reporting spend."""
    if budget.scope == "global":
        rows = receipts.query()
    elif budget.scope == "project":
        rows = receipts.query(project=budget.scope_value)
    else:
        rows = receipts.query(work_id=budget.scope_value)

    window_start = _window_start(budget.window)
    total = Decimal(0)
    has_unpriced_usage = False
    for receipt in rows:
        if receipt.status not in ("success", "partial"):
            continue
        if window_start is not None and receipt.timestamp < window_start:
            continue
        if receipt.estimated_cost_usd is not None:
            total += receipt.estimated_cost_usd
        elif receipt.prompt_tokens is not None and receipt.completion_tokens is not None:
            has_unpriced_usage = True
    return SpentSoFar(spent_usd=total, has_unpriced_usage=has_unpriced_usage)


class BudgetEnforcer:
    """Wired into `InferenceEngine`/`AnthropicInferenceEngine` when
    `InferrailConfig.budgets.enabled` is true. `receipts` is always a real
    `ReceiptsStore` (never `None`) in that case — config validation
    (`InferrailConfig`'s model_validator) refuses to load a config with
    `budgets.enabled: true` and `receipts.sink` other than `sqlite`, since
    spend can't be tracked without an indexed, queryable store."""

    def __init__(
        self,
        store: BudgetStore,
        receipts: ReceiptsStore,
        pricing_resolver: PricingResolver,
        *,
        per_work_default_usd: Decimal | None = None,
        per_work_max_usd: Decimal | None = None,
        allow_declared_budgets: bool = True,
    ) -> None:
        self._store = store
        self._receipts = receipts
        self._pricing_resolver = pricing_resolver
        self._per_work_default_usd = per_work_default_usd
        self._per_work_max_usd = per_work_max_usd
        self._allow_declared_budgets = allow_declared_budgets

    def reserve(
        self,
        *,
        request_id: str,
        provider: str,
        model: str,
        attributes: Mapping[str, str],
        prompt_chars: int,
        max_completion_tokens: int,
        declared_limit_usd: Decimal | None = None,
    ) -> Reservation | None:
        """Admit one provider attempt atomically, or refuse it.

        `declared_limit_usd` is the run's own declared ceiling
        (`X-Inferrail-Budget-Usd`); with it, or with a configured
        `per_work_default_usd`, a per_work budget for the request's work_id
        is created on first use inside this same transaction — see
        docs/adr/0022-per-run-budget-declaration.md.

        Returns the new `Reservation` (to be settled with `release` or
        `hold` once the attempt ends), or `None` when there is nothing to
        reserve against — no matching budget, or an unpriced model under
        warn-mode budgets only. Raises `BudgetExceededError` or
        `BudgetUnpricedModelError` before any provider call.
        """
        estimate = estimate_upper_bound_usd(
            prompt_chars=prompt_chars,
            max_completion_tokens=max_completion_tokens,
            provider=provider,
            model=model,
            pricing_resolver=self._pricing_resolver,
        )
        with self._store.admission() as txn:
            self._apply_per_work_declaration(txn, attributes, declared_limit_usd)
            budgets = matching_budgets(txn.candidate_budgets(attributes), attributes)
            if not budgets:
                return None
            if estimate is None:
                # No price, so no dollar amount to reserve. Never fabricate
                # a $0 estimate: a block budget refuses (fail closed); warn
                # budgets have nothing to project and admit unmetered.
                for budget in budgets:
                    if budget.mode == "block":
                        raise BudgetUnpricedModelError(
                            budget_id=budget.budget_id, provider=provider, model=model
                        )
                return None
            for budget in budgets:
                spent = spent_so_far_usd(self._receipts, budget)
                reserved = txn.reserved_usd(budget, _window_start(budget.window))
                projected = spent.spent_usd + reserved + estimate
                if projected <= budget.limit_usd:
                    continue
                if budget.mode == "block":
                    raise BudgetExceededError(
                        budget_id=budget.budget_id,
                        scope=budget.scope,
                        scope_value=budget.scope_value,
                        window=budget.window,
                        mode=budget.mode,
                        limit_usd=budget.limit_usd,
                        spent_so_far_usd=spent.spent_usd,
                        estimated_request_usd=estimate,
                        projected_total_usd=projected,
                        reserved_usd=reserved,
                    )
                _logger.warning(
                    "budget '%s' (warn mode) would be exceeded: spent $%s + reserved $%s "
                    "+ estimated $%s = $%s > limit $%s",
                    budget.budget_id, spent.spent_usd, reserved, estimate, projected,
                    budget.limit_usd,
                )
            reservation = Reservation(
                reservation_id=f"rsv_{uuid.uuid4().hex[:20]}",
                request_id=request_id,
                project=attributes.get("project"),
                work_id=attributes.get("work_id"),
                amount_usd=estimate,
                state="active",
                created_at=datetime.now(UTC),
            )
            txn.insert(reservation)
            return reservation

    def _apply_per_work_declaration(
        self,
        txn: AdmissionTransaction,
        attributes: Mapping[str, str],
        declared_limit_usd: Decimal | None,
    ) -> None:
        """Resolve a declared or default per-run budget (ADR-0022). The
        first budget for a work_id wins and is never changed by a header;
        a differing declaration is refused, never silently applied."""
        work_id = attributes.get("work_id")
        if declared_limit_usd is not None:
            if not self._allow_declared_budgets:
                raise BudgetDeclarationError(
                    reason="disabled",
                    message="per-run budget declarations are turned off on this gateway "
                    "(budgets.allow_declared_budgets: false)",
                )
            if not work_id:
                raise BudgetDeclarationError(
                    reason="missing_work_id",
                    message="X-Inferrail-Budget-Usd needs X-Inferrail-Attribute-Work-Id: a "
                    "budget is declared for one unit of work",
                )
            if self._per_work_max_usd is not None and declared_limit_usd > self._per_work_max_usd:
                raise BudgetDeclarationError(
                    reason="above_max",
                    message=f"declared budget ${declared_limit_usd} is above this gateway's "
                    f"per-run maximum ${self._per_work_max_usd} (budgets.per_work_max_usd)",
                )
        if not work_id:
            return
        limit = declared_limit_usd if declared_limit_usd is not None else self._per_work_default_usd
        budget_id = new_budget_id("work_id", work_id, "per_work")
        existing = txn.get_budget(budget_id)
        if existing is not None:
            if declared_limit_usd is not None and existing.limit_usd != declared_limit_usd:
                raise BudgetDeclarationError(
                    reason="conflict",
                    message=f"work_id '{work_id}' already has a ${existing.limit_usd} per-run "
                    f"budget; a request declared ${declared_limit_usd}. The first budget for a "
                    "work_id is kept and never changed by a header",
                )
            return
        if limit is None:
            return
        txn.insert_budget(
            Budget(
                budget_id=budget_id,
                scope="work_id",
                scope_value=work_id,
                window="per_work",
                mode="block",
                limit_usd=limit,
            )
        )

    def release(self, reservation: Reservation) -> None:
        """The attempt's cost is committed (its receipt is already written)
        or it was never billed: stop counting the reservation."""
        self._store.release_reservation(reservation.reservation_id)

    def hold(self, reservation: Reservation) -> None:
        """The attempt may have been billed but its cost is unknown: keep
        counting the reserved amount against the budget."""
        self._store.hold_reservation(reservation.reservation_id)

    def augment_overrun(
        self,
        attributes: dict[str, str],
        *,
        provider: str,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        cache: CacheTokens | None = None,
    ) -> dict[str, str]:
        """Post-flight counterpart to `check` — see
        `augment_attributes_with_overrun`'s docstring. Keeps `ReceiptsStore`
        access encapsulated in this class rather than leaking it back out
        to the gateway engines, which only ever need `attributes` in and
        `attributes` (possibly enriched) out."""
        matching = matching_budgets(self._store.list(), attributes)
        if not matching:
            return attributes
        return augment_attributes_with_overrun(
            attributes=attributes,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache=cache,
            matching=matching,
            receipts=self._receipts,
            pricing_resolver=self._pricing_resolver,
        )


def augment_attributes_with_overrun(
    *,
    attributes: dict[str, str],
    provider: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    matching: list[Budget],
    receipts: ReceiptsStore,
    pricing_resolver: PricingResolver,
    cache: CacheTokens | None = None,
) -> dict[str, str]:
    """Post-flight reconciliation: if this request's *actual* cost pushes
    any matching budget over its limit, return `attributes` plus a
    `budget_overrun_usd` entry (the worst overrun across all matching
    budgets). Called before the receipt carrying `attributes` is emitted,
    so `spent_so_far_usd` here still reflects spend *before* this
    request — adding the actual cost gives the correct post-request
    total. Returns `attributes` unchanged (same object) when there's
    nothing to add — no tokens yet, no matching budgets, or no known
    price (including cache tokens that can't be priced exactly)."""
    if not matching or prompt_tokens is None or completion_tokens is None:
        return attributes
    price = pricing_resolver.resolve(provider, model)
    if price is None:
        return attributes
    actual_cost = calculate_cost_with_cache_usd(prompt_tokens, completion_tokens, price, cache)
    if actual_cost is None:
        return attributes

    worst_overrun: Decimal | None = None
    for budget in matching:
        spent_before = spent_so_far_usd(receipts, budget).spent_usd
        projected_total = spent_before + actual_cost
        if projected_total > budget.limit_usd:
            overrun = projected_total - budget.limit_usd
            if worst_overrun is None or overrun > worst_overrun:
                worst_overrun = overrun
    if worst_overrun is None:
        return attributes
    return {**attributes, "budget_overrun_usd": str(worst_overrun)}


def augment_attributes_with_block(
    attributes: dict[str, str], exc: BudgetExceededError | BudgetUnpricedModelError
) -> dict[str, str]:
    """Marks a receipt as a real pre-flight budget block, not just any
    other request failure -- both are `status: "error"` receipts
    otherwise, with nothing on the receipt itself to tell them apart.
    The dashboard's Budgets screen (docs/PRODUCT.md's "Dashboard"
    section) filters on this `budget_id` attribute to build its
    blocked-request log honestly, rather than guessing from error text.
    Same pattern as `augment_attributes_with_overrun`: a system-computed
    key added into the receipt's own generic attributes dict, not a new
    schema field."""
    return {**attributes, "budget_id": exc.budget_id}


def attempt_may_have_billed(exc: BaseException) -> bool:
    """Whether a failed provider attempt may still have been billed —
    decides release vs. hold when no usage came back. Only a provider
    that answered with an HTTP error status (or a local check that fails
    before any network call) is taken as "not billed"; a timeout, a
    transport failure, or a cancellation mid-request may have reached
    the provider, and a malformed 2xx response almost certainly did."""
    if not isinstance(exc, InferrailError):
        return True  # e.g. asyncio.CancelledError: unknown, so conservative
    if isinstance(exc, ProviderTimeoutError):
        return True
    if isinstance(exc, ProviderError) and exc.status_code is not None:
        return exc.status_code < 400
    if isinstance(exc, AuthenticationError):
        return False  # the local missing-key check, raised before any request is sent
    return isinstance(exc, ProviderError)


def settle_as_hold(*, cost_known: bool, may_have_billed: bool) -> bool:
    """The settlement rule (docs/adr/0021): hold only when the provider may
    have billed and there's no priced cost to commit instead."""
    return may_have_billed and not cost_known


def augment_attributes_with_held(attributes: dict[str, str], held_usd: Decimal) -> dict[str, str]:
    """Marks a receipt whose request left reserved dollars held against
    its budget(s) — a system-computed key, like `budget_overrun_usd`. It
    is the reservation amount, deliberately *not* written as the
    receipt's cost: `estimated_cost_usd` stays null when cost is
    unknown."""
    if not held_usd:
        return attributes
    return {**attributes, "budget_held_usd": str(held_usd)}
