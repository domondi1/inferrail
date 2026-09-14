"""Self-serve sandbox tenant issuance for the hosted AP exceptions
service (v0.2.1, see `../../MISSION.md`).

`POST /v1/sandbox` (wired in `service.py`) lets any visitor obtain a
short-lived, isolated, synthetic-data-only credential with no account
and no human in the loop. A sandbox tenant is authenticated exactly like
an operator-provisioned one (`Authorization: Bearer <api-key>`, same
per-tenant SQLite isolation in `tenant_store.py`) -- it differs only in
how the key came to exist, that it expires, that it is capped in size,
and that every response naming it carries an explicit `sandbox: true`
label (see `service.py`'s `_stamp`).

**Abuse guards, all independent of each other:**

- `AP_SANDBOX_ENABLED` -- a kill switch. Set to `false` to refuse all
  new sandbox issuance immediately (existing sandbox tenants already
  issued keep working until they expire).
- `AP_SANDBOX_MAX_LIVE_TENANTS` -- a global ceiling on how many
  not-yet-expired sandbox tenants may exist across all visitors at once.
- `AP_SANDBOX_ISSUE_MAX_PER_IP` / `AP_SANDBOX_ISSUE_WINDOW_SECONDS` -- a
  per-IP throttle on key issuance itself (distinct from the per-tenant
  request rate limit every authenticated route already enforces via
  `auth.RateLimiter`, which applies equally to sandbox and operator
  tenants once a key exists).
- `AP_SANDBOX_MAX_ROWS_PER_TENANT` -- enforced by the caller
  (`service.py`'s `create_decision`), not this module, since it needs
  `RecoveryStore.all_work_ids()`.

Sandbox metadata (which key hashes to which tenant, when it expires, and
which IP requested it) lives in memory only, exactly like
`auth.RateLimiter` -- it resets on process restart, which is acceptable
for a bounded-lifetime credential and avoids adding a second persistent
store next to `tenant_store.py`'s per-tenant SQLite files. A key issued
right before a restart simply stops validating; the visitor requests a
new one.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException

SANDBOX_KEY_PREFIX = "sbx_"

SANDBOX_NOTICE = (
    "sandbox tenant: synthetic data only, isolated from every other tenant, "
    "auto-expires, and is periodically purged -- never use it for real "
    "invoices or real customer data"
)


def _hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


@dataclass(frozen=True)
class SandboxTenant:
    tenant_id: str
    expires_at: float
    created_ip: str

    def is_expired(self, *, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at


class SandboxRegistry:
    """Issues and validates self-serve sandbox credentials for one
    running service process. Thread-safe: FastAPI/uvicorn may serve
    requests from a small thread pool for sync-looking dependencies."""

    def __init__(
        self,
        *,
        enabled: bool,
        ttl_seconds: float,
        max_live_tenants: int,
        issue_max_per_ip: int,
        issue_window_seconds: float,
        purge_grace_seconds: float,
        on_purge: Callable[[str], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.ttl_seconds = ttl_seconds
        self.max_live_tenants = max_live_tenants
        self._issue_max_per_ip = issue_max_per_ip
        self._issue_window_seconds = issue_window_seconds
        self._purge_grace_seconds = purge_grace_seconds
        self._on_purge = on_purge
        self._tenants: dict[str, SandboxTenant] = {}  # key_hash -> tenant
        self._issue_history: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def issue(self, *, client_ip: str) -> tuple[str, SandboxTenant]:
        """Returns `(api_key, tenant)`. Raises `HTTPException` (503/429)
        if any abuse guard trips -- never silently degrades a guard."""
        if not self.enabled:
            raise HTTPException(
                status_code=503,
                detail="sandbox key issuance is currently disabled by the operator",
            )
        with self._lock:
            self._purge_expired_locked()
            history = self._issue_history[client_ip]
            now_mono = time.monotonic()
            while history and now_mono - history[0] > self._issue_window_seconds:
                history.popleft()
            if len(history) >= self._issue_max_per_ip:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"sandbox key issuance limit reached for this address: max "
                        f"{self._issue_max_per_ip} per {self._issue_window_seconds:.0f}s -- "
                        "try again later"
                    ),
                )
            live_tenants = sum(
                1 for t in self._tenants.values() if not t.is_expired()
            )
            if live_tenants >= self.max_live_tenants:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "sandbox capacity is temporarily full -- try again shortly, or "
                        "self-host your own instance (see hosted/ap_exceptions/README.md)"
                    ),
                )
            api_key = SANDBOX_KEY_PREFIX + secrets.token_urlsafe(24)
            key_hash = _hash_key(api_key)
            tenant_id = SANDBOX_KEY_PREFIX + key_hash[:32]
            expires_at = time.time() + self.ttl_seconds
            tenant = SandboxTenant(tenant_id=tenant_id, expires_at=expires_at, created_ip=client_ip)
            self._tenants[key_hash] = tenant
            history.append(now_mono)
            return api_key, tenant

    def lookup(self, api_key: str) -> SandboxTenant | None:
        """Returns the sandbox tenant for `api_key`, whether or not it
        has expired -- the caller (`service.py`) checks `is_expired()`
        itself so it can return a clear "expired" 401 rather than the
        indistinguishable "invalid" 401 an operator key gets. Returns
        `None` for a key this registry never issued, or one purged long
        enough ago that we no longer distinguish it from "never
        issued"."""
        if not api_key.startswith(SANDBOX_KEY_PREFIX):
            return None
        key_hash = _hash_key(api_key)
        with self._lock:
            self._purge_expired_locked()
            return self._tenants.get(key_hash)

    def purge_expired(self) -> list[str]:
        """Public entry point for a periodic background sweep (see
        `service.py`'s startup task) -- same effect as the lazy purge
        every `issue`/`lookup` call already performs, useful for purging
        promptly even when nobody is issuing or using a key."""
        with self._lock:
            return self._purge_expired_locked()

    def _purge_expired_locked(self) -> list[str]:
        now = time.time()
        purge_cutoff = now - self._purge_grace_seconds
        stale_hashes = [
            h for h, t in self._tenants.items() if t.expires_at <= purge_cutoff
        ]
        purged_tenant_ids = []
        for h in stale_hashes:
            tenant = self._tenants.pop(h)
            purged_tenant_ids.append(tenant.tenant_id)
            if self._on_purge is not None:
                self._on_purge(tenant.tenant_id)
        return purged_tenant_ids

    def live_tenant_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tenants.values() if not t.is_expired())


def sandbox_registry_from_env(*, on_purge: Callable[[str], None] | None = None) -> SandboxRegistry:
    return SandboxRegistry(
        enabled=os.environ.get("AP_SANDBOX_ENABLED", "true").strip().lower() != "false",
        ttl_seconds=float(os.environ.get("AP_SANDBOX_TTL_SECONDS", "1800")),
        max_live_tenants=int(os.environ.get("AP_SANDBOX_MAX_LIVE_TENANTS", "200")),
        issue_max_per_ip=int(os.environ.get("AP_SANDBOX_ISSUE_MAX_PER_IP", "5")),
        issue_window_seconds=float(os.environ.get("AP_SANDBOX_ISSUE_WINDOW_SECONDS", "3600")),
        purge_grace_seconds=float(os.environ.get("AP_SANDBOX_PURGE_GRACE_SECONDS", "300")),
        on_purge=on_purge,
    )
