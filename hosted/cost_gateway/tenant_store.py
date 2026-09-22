"""Per-tenant storage isolation for the hosted Cost Gateway.

Same discipline as `hosted/ap_exceptions/tenant_store.py`: every tenant
gets its own SQLite file(s), never a shared table with a `tenant_id`
filter a query could forget. A trial tenant's `tenant_id` (minted in
`trial.py`) is already an opaque, hash-derived string -- it is used
directly as the filename stem, with no further hashing needed.

Each tenant gets **two** SQLite files, mirroring the same split the local
CLI/dashboard already uses (`inferrail serve --app-mode`, see
docs/adr/0016): one `ReceiptsStore` (the payload-free receipt ledger) and
one `BudgetStore` (the tenant's own daily spend cap, wired to a
`BudgetEnforcer` so a trial cannot burn real money), plus one plain JSONL
file for work-outcome declarations (`inferrail.work.builder`'s own
append-only sink, same format `inferrail work outcome` writes locally --
see docs/adr/0008/local `work/` module). All three are opened lazily and
cached for the life of the process; `purge_tenant` deletes every file
(plus WAL/SHM/journal sidecars for the two SQLite ones) for all three.

Never shares a directory, process, or connection with `hosted/work_economics`,
`hosted/a2a_economic_authority`, or `hosted/ap_exceptions` -- see this
service's README, "Isolation from other hosted services."
"""

from __future__ import annotations

import threading
from decimal import Decimal
from pathlib import Path

from inferrail.budgets.enforcement import BudgetEnforcer
from inferrail.budgets.schema import Budget, new_budget_id
from inferrail.budgets.store import BudgetStore
from inferrail.pricing.resolver import PricingResolver
from inferrail.receipts.sqlite_store import ReceiptsStore

DEFAULT_DAILY_BUDGET_USD = Decimal("1.00")
"""A trial tenant's default daily spend cap, in block mode -- small enough
that a mistake or an abusive script cannot run up a meaningful real-money
bill against a visitor's own key before the gateway refuses further
requests. Configurable via `COST_GATEWAY_DAILY_BUDGET_USD` (service.py)."""


class TenantStores:
    """Everything one tenant needs to run real or demo traffic: its own
    receipts ledger, its own budget store, a `BudgetEnforcer` wired to
    both, and the path to its own work-outcomes JSONL file."""

    def __init__(
        self,
        receipts: ReceiptsStore,
        budgets: BudgetStore,
        enforcer: BudgetEnforcer,
        outcomes_path: Path,
    ):
        self.receipts = receipts
        self.budgets = budgets
        self.enforcer = enforcer
        self.outcomes_path = outcomes_path


class TenantStoreRegistry:
    """Lazily opens (and caches, for the life of the process) one
    `TenantStores` per tenant under `data_dir`."""

    def __init__(
        self, data_dir: Path, *, daily_budget_usd: Decimal, pricing_resolver: PricingResolver
    ):
        self._data_dir = data_dir
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._daily_budget_usd = daily_budget_usd
        self._pricing_resolver = pricing_resolver
        self._stores: dict[str, TenantStores] = {}
        self._lock = threading.Lock()

    def get(self, tenant_id: str) -> TenantStores:
        with self._lock:
            stores = self._stores.get(tenant_id)
            if stores is not None:
                return stores
            receipts = ReceiptsStore(self._data_dir / f"{tenant_id}-receipts.sqlite3")
            budgets = BudgetStore(self._data_dir / f"{tenant_id}-budgets.sqlite3")
            budget_id = new_budget_id("global", None, "daily")
            if budgets.get(budget_id) is None:
                budgets.set(
                    Budget(
                        budget_id=budget_id,
                        scope="global",
                        scope_value=None,
                        window="daily",
                        mode="block",
                        limit_usd=self._daily_budget_usd,
                    )
                )
            enforcer = BudgetEnforcer(budgets, receipts, self._pricing_resolver)
            outcomes_path = self._data_dir / f"{tenant_id}-work-outcomes.jsonl"
            stores = TenantStores(receipts, budgets, enforcer, outcomes_path)
            self._stores[tenant_id] = stores
            return stores

    def purge_tenant(self, tenant_id: str) -> None:
        """Irreversibly deletes one tenant's receipts and budget SQLite
        files (plus sidecars) and its work-outcomes JSONL file, and drops
        it from the in-process cache. Neither SQLite store holds a
        long-lived connection open (both open/close per call), so there
        is nothing to close first."""
        with self._lock:
            self._stores.pop(tenant_id, None)
        for stem in (f"{tenant_id}-receipts.sqlite3", f"{tenant_id}-budgets.sqlite3"):
            db_path = self._data_dir / stem
            for suffix in ("", "-wal", "-shm", "-journal"):
                candidate = db_path.parent / (db_path.name + suffix)
                candidate.unlink(missing_ok=True)
        (self._data_dir / f"{tenant_id}-work-outcomes.jsonl").unlink(missing_ok=True)
