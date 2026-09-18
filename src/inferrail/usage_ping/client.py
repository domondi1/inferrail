"""Sends one usage-ping event, if and only if it's actually on.

`maybe_send_event` is the one function every integration point calls,
unconditionally -- it is always cheap and always safe:

- If usage_ping is disabled, no `endpoint` is configured, or the
  environment says not to (see `_disabled_by_environment`), it returns
  immediately with no network access at all (not even a DNS lookup).
- Otherwise, for the two once-ever events (`install`, `first_receipt`) it
  checks a local, idempotent "already sent" marker (a fast file read) and
  only sends the first time; `serve_start` is never deduped (it fires
  once per process start, by construction -- see `gateway/app.py`).
- The network call can never raise into the caller, block it, or slow it
  beyond the local marker check: the actual POST always happens on a
  background thread the caller never waits on, and any failure there
  (offline, DNS failure, timeout, a non-2xx response) is caught and
  logged at debug level only, never surfaced.

`heartbeat` is handled by the separate `maybe_send_heartbeat`/
`start_heartbeat_thread` below -- its "at most once per 24 hours while
serving" cadence doesn't fit the once-ever/every-time shape the other
three events use.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from inferrail.config.models import UsagePingConfig
from inferrail.usage_ping import state as ping_state
from inferrail.usage_ping.install_id import ensure_install_id
from inferrail.usage_ping.payload import build_payload
from inferrail.usage_ping.state import HEARTBEAT_MIN_INTERVAL

_logger = logging.getLogger("inferrail.usage_ping")
_SEND_TIMEOUT_SECONDS = 3.0
_HEARTBEAT_CHECK_INTERVAL_SECONDS = 3600.0

_ONCE_EVER_EVENTS = frozenset({"install", "first_receipt"})

# Common, well-known signals that this process is not a real end-user
# install: a CI runner, an explicit "do not track" request (the
# https://consoledonottrack.com/ convention), or this project's own test
# suite. Checked fresh on every call, not cached -- these are cheap
# lookups and callers may run inside subprocess-spawned test workers with
# their own environment.
def _disabled_by_environment() -> bool:
    if os.environ.get("INFERRAIL_TELEMETRY") == "0":
        return True
    if os.environ.get("DO_NOT_TRACK") == "1":
        return True
    if os.environ.get("PYTEST_CURRENT_TEST") is not None:
        return True
    if os.environ.get("CI", "").strip().lower() in ("1", "true", "yes"):
        return True
    return False


def _send_in_background(endpoint: str, payload: dict[str, str], *, name: str) -> None:
    def _send() -> None:
        try:
            httpx.post(endpoint, json=payload, timeout=_SEND_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - must never propagate; offline is normal.
            _logger.debug("usage ping %s: send failed (non-fatal)", name, exc_info=True)

    threading.Thread(target=_send, daemon=True, name=f"usage-ping-{name}").start()


def maybe_send_event(event: str, *, app_data_dir: Path, config: UsagePingConfig) -> None:
    if not config.endpoint:
        return  # Not yet active -- no collector configured. Never even checked further.
    if _disabled_by_environment():
        return

    state = ping_state.load_state(app_data_dir, default_enabled=config.enabled)
    if not state.enabled:
        return

    if event in _ONCE_EVER_EVENTS:
        now = datetime.now(UTC).isoformat()
        if not ping_state.mark_event_sent_if_new(app_data_dir, event, ts=now):
            return  # Already sent this milestone for this install.

    install_id = ensure_install_id(app_data_dir)
    payload = build_payload(event, install_id)
    _send_in_background(config.endpoint, payload, name=event)


def maybe_send_heartbeat(
    *, app_data_dir: Path, config: UsagePingConfig, now: datetime | None = None
) -> None:
    """Sends a `heartbeat` event if and only if at least
    `HEARTBEAT_MIN_INTERVAL` has elapsed since the last one this install
    actually sent (or none has ever been sent) -- see
    `usage_ping.state.mark_heartbeat_sent_if_due`. Same disabled/endpoint
    gates as `maybe_send_event`."""
    if not config.endpoint:
        return
    if _disabled_by_environment():
        return

    state = ping_state.load_state(app_data_dir, default_enabled=config.enabled)
    if not state.enabled:
        return

    if not ping_state.mark_heartbeat_sent_if_due(app_data_dir, now=now or datetime.now(UTC)):
        return

    install_id = ensure_install_id(app_data_dir)
    payload = build_payload("heartbeat", install_id)
    _send_in_background(config.endpoint, payload, name="heartbeat")


def start_heartbeat_thread(*, app_data_dir: Path, config: UsagePingConfig) -> None:
    """Starts a best-effort daemon thread that wakes roughly hourly and
    calls `maybe_send_heartbeat` (which itself enforces the real
    `HEARTBEAT_MIN_INTERVAL` gate) -- the mechanism behind "at most once
    per 24 hours while serving" for a long-running `inferrail serve`
    process that might otherwise see zero request traffic for a day.

    Deliberately never started at all when usage-ping couldn't possibly
    be active (no endpoint configured, or the environment says not to) --
    so test suites, CI runs, and the overwhelming majority of installs
    that never configure `usage_ping.endpoint` never spawn this thread in
    the first place, not just never send from it.
    """
    if not config.endpoint or _disabled_by_environment():
        return

    def _loop() -> None:
        while True:
            time.sleep(_HEARTBEAT_CHECK_INTERVAL_SECONDS)
            maybe_send_heartbeat(app_data_dir=app_data_dir, config=config)

    threading.Thread(target=_loop, daemon=True, name="usage-ping-heartbeat").start()


__all__ = [
    "HEARTBEAT_MIN_INTERVAL",
    "maybe_send_event",
    "maybe_send_heartbeat",
    "start_heartbeat_thread",
]
