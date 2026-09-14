"""`inferrail budget set|list|rm`: CRUD over the `BudgetStore` (see
docs/adr/0015-budget-enforcement.md).

Deliberately independent of whether `budgets.enabled` is set in
`inferrail.yaml` — an operator can author/inspect budgets via this CLI
before ever turning enforcement on, and turning enforcement off doesn't
delete anything already stored.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

from inferrail.budgets.schema import Budget, BudgetMode, BudgetScope, BudgetWindow, new_budget_id
from inferrail.budgets.store import BudgetStore


def run_budget_set(
    db_path: Path,
    *,
    scope: BudgetScope,
    scope_value: str | None,
    window: BudgetWindow,
    mode: BudgetMode,
    limit_usd: str,
) -> int:
    try:
        limit = Decimal(limit_usd)
    except InvalidOperation:
        print(f"error: --limit-usd '{limit_usd}' is not a valid number", file=sys.stderr)
        return 1
    budget_id = new_budget_id(scope, scope_value, window)
    try:
        budget = Budget(
            budget_id=budget_id,
            scope=scope,
            scope_value=scope_value,
            window=window,
            mode=mode,
            limit_usd=limit,
        )
    except ValidationError as exc:
        print(f"error: {exc}")
        return 1

    store = BudgetStore(db_path)
    existed = store.get(budget_id) is not None
    store.set(budget)
    verb = "Updated" if existed else "Created"
    scoped = f"{scope}={scope_value}" if scope_value else scope
    print(f"{verb} budget '{budget_id}' ({scoped}, {window}, mode={mode}, limit=${limit}).")
    return 0


def run_budget_list(db_path: Path, *, as_json: bool) -> int:
    if not db_path.exists():
        if as_json:
            print("[]")
        else:
            print(f"No budget store found at {db_path} (no budgets set yet).")
        return 0
    store = BudgetStore(db_path)
    budgets = store.list()
    if as_json:
        print(json.dumps([b.model_dump(mode="json") for b in budgets], indent=2))
        return 0
    if not budgets:
        print(f"No budgets set in {db_path}.")
        return 0
    for budget in budgets:
        scoped = f"{budget.scope}={budget.scope_value}" if budget.scope_value else budget.scope
        print(
            f"{budget.budget_id}  {scoped}  window={budget.window}  mode={budget.mode}  "
            f"limit=${budget.limit_usd}"
        )
    return 0


def run_budget_rm(db_path: Path, budget_id: str) -> int:
    if not db_path.exists():
        print(f"No budget store found at {db_path}.")
        return 1
    store = BudgetStore(db_path)
    removed = store.remove(budget_id)
    if not removed:
        print(f"No budget '{budget_id}' found in {db_path}.")
        return 1
    print(f"Removed budget '{budget_id}'.")
    return 0
