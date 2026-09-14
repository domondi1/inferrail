"""The `Budget` entity: a spend cap scoped to global/project/work_id, over
a window, in warn or block mode.

A `Budget` is data, not behavior — see `inferrail.budgets.enforcement` for
what actually checks a request against one, and `inferrail.budgets.store`
for where instances live. Kept as its own tiny schema module (mirroring
`config.models`' style) so it can be imported by the CLI, the store, and
the gateway without pulling in enforcement logic or SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

BudgetScope = Literal["global", "project", "work_id"]
BudgetWindow = Literal["per_work", "daily", "monthly"]
BudgetMode = Literal["warn", "block"]


class Budget(BaseModel):
    """One spend cap.

    `budget_id` is deterministic — `f"{scope}:{scope_value or '_'}:{window}"`
    (see `new_budget_id`) — so `inferrail budget set` for the same
    (scope, scope_value, window) triple is naturally an upsert, never a
    silently-accumulating duplicate. This is also what makes `budget
    set|list|rm` a coherent CRUD surface without a separate id-lookup step.
    """

    model_config = {"extra": "forbid"}

    budget_id: str
    scope: BudgetScope
    # None for scope="global"; the project name or work_id being scoped to
    # otherwise. Never an empty string — see `_validate_scope_shape`.
    scope_value: str | None = None
    window: BudgetWindow
    mode: BudgetMode
    limit_usd: Decimal = Field(gt=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _validate_scope_shape(self) -> Budget:
        if self.scope == "global" and self.scope_value is not None:
            raise ValueError("a 'global' budget must not set scope_value")
        if self.scope in ("project", "work_id") and not self.scope_value:
            raise ValueError(f"a '{self.scope}' budget requires a non-empty scope_value")
        if self.window == "per_work" and self.scope != "work_id":
            raise ValueError(
                "window 'per_work' only makes sense for scope='work_id' — a "
                "global/project budget with no time boundary would never reset"
            )
        expected_id = new_budget_id(self.scope, self.scope_value, self.window)
        if self.budget_id != expected_id:
            raise ValueError(
                f"budget_id '{self.budget_id}' does not match the deterministic id "
                f"for this scope/scope_value/window ('{expected_id}')"
            )
        return self


def new_budget_id(scope: BudgetScope, scope_value: str | None, window: BudgetWindow) -> str:
    """The deterministic identity of a budget: one row per
    (scope, scope_value, window) triple. `inferrail budget set` computes
    this to decide whether it's creating a new budget or replacing an
    existing one at the same scope."""
    return f"{scope}:{scope_value or '_'}:{window}"
