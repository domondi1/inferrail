"""Self-serve trial-tenant issuance for the hosted Cost Gateway.

`POST /v1/trial` (wired in `service.py`) lets any visitor obtain a
short-lived, isolated credential with no account and no human in the
loop -- the same shape `hosted/ap_exceptions/sandbox.py` already proved
in production, adapted for two differences specific to this service:

1. **Two TTLs, not one.** A trial tenant is issued with a **24 hour**
   demo-mode expiry. If and when the tenant submits a real provider key
   (`keys.py`), its expiry is *shortened* -- never extended -- to at
   most **4 hours from the moment the key was submitted**, because a
   live provider key sitting in process memory is a larger blast radius
   than synthetic demo receipts. See `Tenant.tighten_for_real_key`.
2. **The expiry is meant to be shown to the visitor continuously**, not
   just checked server-side -- `GET /v1/trial/{tenant_id}` (service.py)
   reports `expires_at` and `seconds_remaining` on every call so the
   dashboard/provisioning UI can render a live countdown, per explicit
   founder instruction that trial deletion must be highly visible.

Abuse guards mirror `hosted/ap_exceptions/sandbox.py` exactly (same
env-var-driven, independently-testable shape): a kill switch, a global
live-tenant ceiling, and a per-IP issuance throttle. Tenant metadata
lives in memory only, like that module's -- it resets on process
restart, which is acceptable for a bounded-lifetime credential and keeps
this service from needing a second persistent store next to
`tenant_store.py`'s per-tenant SQLite files.
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

TRIAL_KEY_PREFIX = "trial_"

DEMO_TTL_SECONDS_DEFAULT = 24 * 60 * 60  # 24 hours -- founder-confirmed default
REAL_KEY_TTL_SECONDS_DEFAULT = 4 * 60 * 60  # 4 hours -- founder-confirmed default

TRIAL_NOTICE = (
    "trial tenant: isolated from every other tenant, auto-expires, and is "
    "periodically purged -- demo-mode data lives for up to 24 hours; once a "
    "real provider key is added, this trial (and that key) expires within "
    "4 hours of the key being added, whichever comes first"
)


def _hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


@dataclass
class Tenant:
    tenant_id: str
    created_at: float
    expires_at: float
    created_ip: str
    has_real_key: bool = False

    def is_expired(self, *, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def seconds_remaining(self, *, now: float | None = None) -> float:
        return max(0.0, self.expires_at - (now if now is not None else time.time()))

    def tighten_for_real_key(self, *, submitted_at: float, real_key_ttl_seconds: float) -> None:
        """Called once per key submission (service.py). Never extends
        `expires_at` -- only ever shortens it, and only takes effect the
        first time a real key is added (a second key submission recomputes
        against the *original* submission's cap implicitly, since
        `expires_at` monotonically only gets earlier here, never later)."""
        self.has_real_key = True
        candidate = submitted_at + real_key_ttl_seconds
        self.expires_at = min(self.expires_at, candidate)


class TrialRegistry:
    """Issues and validates self-serve trial credentials for one running
    service process. Thread-safe, same reasoning as
    `hosted/ap_exceptions/sandbox.py`'s `SandboxRegistry`."""

    def __init__(
        self,
        *,
        enabled: bool,
        demo_ttl_seconds: float,
        real_key_ttl_seconds: float,
        max_live_tenants: int,
        issue_max_per_ip: int,
        issue_window_seconds: float,
        purge_grace_seconds: float,
        on_purge: Callable[[str], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.demo_ttl_seconds = demo_ttl_seconds
        self.real_key_ttl_seconds = real_key_ttl_seconds
        self.max_live_tenants = max_live_tenants
        self._issue_max_per_ip = issue_max_per_ip
        self._issue_window_seconds = issue_window_seconds
        self._purge_grace_seconds = purge_grace_seconds
        self._on_purge = on_purge
        self._tenants: dict[str, Tenant] = {}  # key_hash -> tenant
        self._issue_history: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._total_issued = 0
        """All-time counter, process-local (reset on restart, same as
        every other in-memory state in this service -- see the README's
        "Known limitations"). Deliberately a count of *trials issued*,
        not "unique users": there is no account system yet, so nothing
        in this service can distinguish one visitor starting two trials
        from two different visitors. State that plainly wherever this
        number is surfaced -- see `service.py`'s `/v1/admin/stats`."""

    def issue(self, *, client_ip: str) -> tuple[str, Tenant]:
        """Returns `(api_key, tenant)`. Raises `HTTPException` (503/429)
        if any abuse guard trips -- never silently degrades a guard."""
        if not self.enabled:
            raise HTTPException(
                status_code=503,
                detail="trial issuance is currently disabled by the operator",
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
                        f"trial issuance limit reached for this address: max "
                        f"{self._issue_max_per_ip} per {self._issue_window_seconds:.0f}s -- "
                        "try again later"
                    ),
                )
            live_tenants = sum(1 for t in self._tenants.values() if not t.is_expired())
            if live_tenants >= self.max_live_tenants:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "trial capacity is temporarily full -- try again shortly, or "
                        "self-host your own instance (see hosted/cost_gateway/README.md, "
                        "or `pip install inferrail && inferrail serve --quickstart`)"
                    ),
                )
            api_key = TRIAL_KEY_PREFIX + secrets.token_urlsafe(24)
            key_hash = _hash_key(api_key)
            tenant_id = TRIAL_KEY_PREFIX + key_hash[:32]
            now = time.time()
            tenant = Tenant(
                tenant_id=tenant_id,
                created_at=now,
                expires_at=now + self.demo_ttl_seconds,
                created_ip=client_ip,
            )
            self._tenants[key_hash] = tenant
            history.append(now_mono)
            self._total_issued += 1
            return api_key, tenant

    def lookup(self, api_key: str) -> Tenant | None:
        """Returns the trial tenant for `api_key`, whether or not it has
        expired -- the caller (`service.py`) checks `is_expired()` itself
        so it can return a clear "expired" 401 rather than the
        indistinguishable "invalid" 401 a malformed key gets."""
        if not api_key.startswith(TRIAL_KEY_PREFIX):
            return None
        key_hash = _hash_key(api_key)
        with self._lock:
            self._purge_expired_locked()
            return self._tenants.get(key_hash)

    def lookup_by_tenant_id(self, tenant_id: str) -> Tenant | None:
        with self._lock:
            self._purge_expired_locked()
            for tenant in self._tenants.values():
                if tenant.tenant_id == tenant_id:
                    return tenant
            return None

    def purge_expired(self) -> list[str]:
        """Public entry point for a periodic background sweep (see
        `service.py`'s startup task)."""
        with self._lock:
            return self._purge_expired_locked()

    def end_trial(self, tenant_id: str) -> bool:
        """Explicit, immediate teardown (not just marking expired) --
        used by `DELETE /v1/trial/{tenant_id}` for a visitor who wants
        their trial (and any key/data) gone right now rather than waiting
        for the TTL. Returns whether a live tenant was actually found and
        removed."""
        with self._lock:
            key_hash = next(
                (h for h, t in self._tenants.items() if t.tenant_id == tenant_id), None
            )
            if key_hash is None:
                return False
            tenant = self._tenants.pop(key_hash)
            if self._on_purge is not None:
                self._on_purge(tenant.tenant_id)
            return True

    def _prune_issue_history_locked(self) -> None:
        """Drops per-IP issuance histories with nothing left inside the
        window, so this table's size tracks recent issuers rather than
        every address ever seen."""
        now_mono = time.monotonic()
        stale = [
            ip
            for ip, history in self._issue_history.items()
            if not history or now_mono - history[-1] > self._issue_window_seconds
        ]
        for ip in stale:
            del self._issue_history[ip]

    def tracked_ip_count(self) -> int:
        with self._lock:
            return len(self._issue_history)

    def _purge_expired_locked(self) -> list[str]:
        self._prune_issue_history_locked()
        now = time.time()
        purge_cutoff = now - self._purge_grace_seconds
        stale_hashes = [h for h, t in self._tenants.items() if t.expires_at <= purge_cutoff]
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

    def total_issued_count(self) -> int:
        with self._lock:
            return self._total_issued


def trial_registry_from_env(*, on_purge: Callable[[str], None] | None = None) -> TrialRegistry:
    return TrialRegistry(
        enabled=os.environ.get("COST_GATEWAY_TRIAL_ENABLED", "true").strip().lower() != "false",
        demo_ttl_seconds=float(
            os.environ.get("COST_GATEWAY_DEMO_TTL_SECONDS", str(DEMO_TTL_SECONDS_DEFAULT))
        ),
        real_key_ttl_seconds=float(
            os.environ.get(
                "COST_GATEWAY_REAL_KEY_TTL_SECONDS", str(REAL_KEY_TTL_SECONDS_DEFAULT)
            )
        ),
        max_live_tenants=int(os.environ.get("COST_GATEWAY_MAX_LIVE_TENANTS", "500")),
        issue_max_per_ip=int(os.environ.get("COST_GATEWAY_ISSUE_MAX_PER_IP", "5")),
        issue_window_seconds=float(
            os.environ.get("COST_GATEWAY_ISSUE_WINDOW_SECONDS", "3600")
        ),
        purge_grace_seconds=float(os.environ.get("COST_GATEWAY_PURGE_GRACE_SECONDS", "300")),
        on_purge=on_purge,
    )
