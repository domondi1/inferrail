"""HTTP schemas for the local control API — see
docs/adr/0016-local-control-api.md.

Deliberately thin: `InferenceReceipt`, `WorkSummary`, and `Budget`
(the domain models this API reads/writes) are reused directly as
response bodies rather than duplicated here. Only the shapes those
models don't already cover — a paginated envelope, a budget-creation
request without a client-supplied `budget_id` — get their own type.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from inferrail.budgets.schema import Budget, BudgetMode, BudgetScope, BudgetWindow
from inferrail.receipts.schema import InferenceReceipt


class ReceiptsPage(BaseModel):
    receipts: list[InferenceReceipt]
    total: int
    limit: int
    offset: int


class BudgetSpend(BaseModel):
    """Response body for `GET /v1/local/budgets/spend` -- one entry per
    configured budget, reusing `budgets.enforcement.spent_so_far_usd`
    directly (the same function `BudgetEnforcer.check` itself uses) so
    the dashboard's burn bar can never drift from what enforcement
    actually computes. `has_unpriced_usage=true` means this budget's
    spend is a floor, not the true total -- rendered distinctly, never
    silently treated as complete (docs/PRODUCT.md's honest-numbers
    rule)."""

    budget_id: str
    limit_usd: Decimal
    spent_usd: Decimal
    has_unpriced_usage: bool


class OutcomeRequest(BaseModel):
    """Request body for `POST /v1/local/ap/{work_id}/outcome` — the same
    shape as `hosted/ap_exceptions/service.py`'s own `OutcomeRequest`
    (not imported from there: `hosted/` is a separate deployable with
    its own dependency set, never a dependency of the installed
    `inferrail` package). `correction_delta_usd`/`review_cost_usd` are
    left as `str | None`, not `Decimal`, matching
    `ap.store.RecoveryStore.record_outcome`'s own signature exactly —
    no reparsing between the two."""

    model_config = {"extra": "forbid"}

    outcome: str = Field(min_length=1)
    source: str = "unknown"
    correction_delta_usd: str | None = None
    review_cost_usd: str | None = None


class BudgetCreate(BaseModel):
    """Request body for `POST /v1/local/budgets`. No `budget_id` field —
    it's computed server-side from scope/scope_value/window
    (`budgets.schema.new_budget_id`), the same rule `inferrail budget
    set` uses, so a client can't supply a mismatched or colliding one."""

    model_config = {"extra": "forbid"}

    scope: BudgetScope
    scope_value: str | None = None
    window: BudgetWindow
    mode: BudgetMode
    limit_usd: Decimal


__all__ = ["Budget", "BudgetCreate", "BudgetSpend", "OutcomeRequest", "ReceiptsPage"]
