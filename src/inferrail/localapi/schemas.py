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

from pydantic import BaseModel

from inferrail.budgets.schema import Budget, BudgetMode, BudgetScope, BudgetWindow
from inferrail.receipts.schema import InferenceReceipt


class ReceiptsPage(BaseModel):
    receipts: list[InferenceReceipt]
    total: int
    limit: int
    offset: int


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


__all__ = ["Budget", "BudgetCreate", "ReceiptsPage"]
