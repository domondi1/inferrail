"""Sends one usage-ping event, if and only if it's actually on.

`maybe_send_event` is the one function every integration point calls,
unconditionally -- it is always cheap and always safe:

- If usage_ping is disabled, or no `endpoint` is configured, it returns
  immediately with no network access at all (not even a DNS lookup).
- Otherwise it checks a local, idempotent "already sent" marker (a fast
  file read) and, only on the *first* time this event fires for this
  install, spawns a daemon thread that POSTs the payload with a short
  timeout.
- The network call can never raise into the caller, block it, or slow it
  beyond the local marker check: the actual POST always happens on a
  background thread the caller never waits on, and any failure there
  (offline, DNS failure, timeout, a non-2xx response) is caught and
  logged at debug level only, never surfaced.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

import httpx

from inferrail.config.models import UsagePingConfig
from inferrail.usage_ping import state as ping_state
from inferrail.usage_ping.install_id import ensure_install_id
from inferrail.usage_ping.payload import build_payload

_logger = logging.getLogger("inferrail.usage_ping")
_SEND_TIMEOUT_SECONDS = 3.0


def maybe_send_event(event: str, *, app_data_dir: Path, config: UsagePingConfig) -> None:
    if not config.endpoint:
        return  # Not yet active -- no collector configured. Never even checked further.

    state = ping_state.load_state(app_data_dir, default_enabled=config.enabled)
    if not state.enabled:
        return

    now = datetime.now(UTC).isoformat()
    if not ping_state.mark_event_sent_if_new(app_data_dir, event, ts=now):
        return  # Already sent this milestone for this install.

    install_id = ensure_install_id(app_data_dir)
    payload = build_payload(event, install_id)
    endpoint = config.endpoint

    def _send() -> None:
        try:
            httpx.post(endpoint, json=payload, timeout=_SEND_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - must never propagate; offline is normal.
            _logger.debug("usage ping %s: send failed (non-fatal)", event, exc_info=True)

    threading.Thread(target=_send, daemon=True, name=f"usage-ping-{event}").start()
