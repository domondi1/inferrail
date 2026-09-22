"""In-memory-only custody of visitor-supplied provider API keys.

**Threat model, restated here on purpose** (every module that touches key
handling in this service must restate it — see the private planning
record this service was authorized from):

- **Passive log/telemetry leakage:** a key ends up in an access log,
  error log, crash report, or third-party telemetry by accident. Mitigated
  structurally: this module never logs a raw key anywhere, `redacted()`
  is the only representation safe to put in a log line or an error
  message, and no other module in this service imports `_KeyVaultEntry`
  or reaches into `KeyVault._keys` directly.
- **Data-at-rest compromise:** the hosted database/disk is compromised.
  Mitigated by construction: a key is held only in this process's memory
  (a plain `dict`), never written to `tenant_store.py`'s SQLite files,
  never included in a receipt, and never exported. A process restart or
  crash loses every key in memory -- an accepted, documented trade-off
  (see this service's README), not an oversight.
- **Cross-tenant leakage:** a bug lets one tenant's request handler read
  or use another tenant's key. Mitigated by keying this store strictly by
  `tenant_id` and never exposing a bulk/iteration accessor that a caller
  could misuse across tenants -- see `test_service.py`'s adversarial
  cross-tenant test.
- **Over-broad use:** a key is used for something other than proxying
  that same tenant's own request. Mitigated by `service.py` only ever
  reading a key immediately before constructing a per-request Provider
  for that tenant's own proxied call, never storing it on a
  longer-lived object, never forwarding it to any third party.

A key is never echoed back in any API response -- every route that
reports which providers are configured for a tenant reports only
booleans (`openai_configured: true/false`), never the value.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

ProviderName = str  # "openai" | "anthropic"

_REDACTED = "<redacted, never persisted, never logged>"


@dataclass
class _KeyVaultEntry:
    openai_key: str | None = None
    anthropic_key: str | None = None
    submitted_at: float | None = None


@dataclass(frozen=True)
class KeyStatus:
    """Safe-to-return-in-an-API-response summary of a tenant's key state --
    booleans only, never a value, never even a prefix/suffix of one."""

    openai_configured: bool
    anthropic_configured: bool
    submitted_at: float | None


class KeyVault:
    """Holds real provider API keys for live trial tenants, in process
    memory only. Thread-safe: FastAPI/uvicorn may serve requests from a
    small thread pool for sync-looking dependencies, same reasoning as
    `hosted/ap_exceptions/sandbox.py`'s `SandboxRegistry`."""

    def __init__(self) -> None:
        self._keys: dict[str, _KeyVaultEntry] = {}
        self._lock = threading.Lock()

    def set_keys(
        self, tenant_id: str, *, openai_key: str | None = None, anthropic_key: str | None = None
    ) -> float:
        """Stores whichever of `openai_key`/`anthropic_key` is not None,
        preserving any previously-stored key for the other provider (a
        tenant may submit an OpenAI key now and an Anthropic key later --
        this is additive, never a full replace). Returns the submission
        timestamp, used by `trial.py` to (re)compute this tenant's
        real-key expiry."""
        now = time.time()
        with self._lock:
            entry = self._keys.get(tenant_id)
            if entry is None:
                entry = _KeyVaultEntry()
                self._keys[tenant_id] = entry
            if openai_key is not None:
                entry.openai_key = openai_key
            if anthropic_key is not None:
                entry.anthropic_key = anthropic_key
            entry.submitted_at = now
        return now

    def get_openai_key(self, tenant_id: str) -> str | None:
        with self._lock:
            entry = self._keys.get(tenant_id)
            return entry.openai_key if entry is not None else None

    def get_anthropic_key(self, tenant_id: str) -> str | None:
        with self._lock:
            entry = self._keys.get(tenant_id)
            return entry.anthropic_key if entry is not None else None

    def status(self, tenant_id: str) -> KeyStatus:
        with self._lock:
            entry = self._keys.get(tenant_id)
            if entry is None:
                return KeyStatus(
                    openai_configured=False, anthropic_configured=False, submitted_at=None
                )
            return KeyStatus(
                openai_configured=entry.openai_key is not None,
                anthropic_configured=entry.anthropic_key is not None,
                submitted_at=entry.submitted_at,
            )

    def forget(self, tenant_id: str) -> bool:
        """Irreversibly discards every key held for `tenant_id`. Returns
        whether anything was actually held. Used both by the explicit
        `DELETE /v1/trial/{tenant_id}/keys` route and by trial
        expiry/teardown -- a key must never outlive its tenant."""
        with self._lock:
            return self._keys.pop(tenant_id, None) is not None

    @staticmethod
    def redacted() -> str:
        """The only string representation of a key state safe to put in a
        log line or error message -- never the key itself."""
        return _REDACTED
