"""Per-tenant storage isolation for the hosted AP exceptions service.

Each authenticated API key is its own tenant. Isolation is enforced at
the filesystem level, not by a row-level `tenant_id` filter a query could
forget: every tenant gets its own SQLite file, named by a one-way hash of
its API key so the key itself is never used as or embedded in a
filename. A bug in one tenant's request handling cannot read or write
another tenant's rows, because there is no shared table to query across
tenants in the first place.

Separate from, and never sharing a directory or process with,
`hosted/work_economics/store.py` or
`hosted/a2a_economic_authority`'s stores -- see this service's README,
"Isolation from other hosted services."
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from inferrail.ap.store import RecoveryStore


def tenant_id_for_api_key(api_key: str) -> str:
    """A stable, one-way identifier for an API key -- used as the
    filename stem and in logs, never the raw key itself."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


class TenantStoreRegistry:
    """Lazily opens (and caches, for the life of the process) one
    `RecoveryStore` per tenant under `data_dir`."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._stores: dict[str, RecoveryStore] = {}
        self._lock = threading.Lock()

    def get(self, tenant_id: str) -> RecoveryStore:
        with self._lock:
            store = self._stores.get(tenant_id)
            if store is None:
                store = RecoveryStore(self._data_dir / f"{tenant_id}.sqlite3")
                self._stores[tenant_id] = store
            return store
