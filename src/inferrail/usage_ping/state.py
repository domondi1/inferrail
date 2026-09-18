"""The mutable, runtime on/off state for the usage ping, plus per-event
"already sent" bookkeeping -- kept separate from `inferrail.yaml` for the
same reason `BudgetStore` is separate from config (docs/adr/0015): a
config file is loaded once at startup, but the dashboard's Settings
toggle (and `inferrail telemetry enable|disable`) need to flip this live,
from a possibly different process than the one running `inferrail serve`.

`UsagePingConfig.enabled` (inferrail.yaml) only seeds this file's initial
value the first time it's created; after that, this file is the one
source of truth for whether the ping is on. `endpoint` is deliberately
*not* stored or settable here -- it stays operator/config-controlled only
(see `docs/adr/0019-opt-in-usage-ping.md`'s "why the dashboard can't set
the endpoint").

As of
docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md, this
default posture is **opt-out**, not opt-in -- a deliberate, explicitly
recorded reversal of ADR-0019's "default off" decision, at the founder's
direction. See that ADR for the full reasoning; nothing else about the
mechanics below changed because of it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

_STATE_FILENAME = "usage-ping-state.json"

# Exactly the four lifecycle events docs/adr/0020 specifies -- matches the
# collector's own `CHECK (event IN (...))` constraint
# (hosted/usage_ping/service.py) verbatim. `install` and `first_receipt`
# fire at most once per install, ever; `serve_start` fires once per
# process start; `heartbeat` fires at most once per 24 hours while a
# server process is running (see `mark_heartbeat_sent_if_due` below).
KNOWN_EVENTS = ("install", "serve_start", "first_receipt", "heartbeat")

HEARTBEAT_MIN_INTERVAL = timedelta(hours=24)


class UsagePingState(BaseModel):
    model_config = {"extra": "forbid"}

    # Opt-out default (docs/adr/0020) -- but still fully inert with no
    # `usage_ping.endpoint` configured, regardless of this value (see
    # `usage_ping.client.maybe_send_event`'s first check).
    enabled: bool = True
    # event name -> ISO timestamp it was first sent, or absent if never
    # sent. Only ever holds "once-ever" events (install, first_receipt) --
    # serve_start is never deduped here, and heartbeat uses
    # `last_heartbeat_at` below instead, since its dedup window is 24
    # hours, not "forever".
    sent_events: dict[str, str] = {}
    # ISO timestamp of the last heartbeat actually sent, or None if never.
    last_heartbeat_at: str | None = None


def _state_path(app_data_dir: Path) -> Path:
    return app_data_dir / _STATE_FILENAME


def load_state(app_data_dir: Path, *, default_enabled: bool = True) -> UsagePingState:
    """Reads the state file, creating it (seeded from `default_enabled`,
    the config file's `usage_ping.enabled`) on first use."""
    path = _state_path(app_data_dir)
    if not path.exists():
        state = UsagePingState(enabled=default_enabled)
        save_state(app_data_dir, state)
        return state
    try:
        return UsagePingState.model_validate_json(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        # A hand-edited or corrupted file must never crash the gateway --
        # treat it as "never configured" and let the next save() repair it.
        return UsagePingState(enabled=default_enabled)


def save_state(app_data_dir: Path, state: UsagePingState) -> None:
    app_data_dir.mkdir(parents=True, exist_ok=True)
    path = _state_path(app_data_dir)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(state.model_dump_json(), encoding="utf-8")
    os.replace(tmp_path, path)  # atomic on POSIX and Windows


def mark_event_sent_if_new(app_data_dir: Path, event: str, *, ts: str) -> bool:
    """Records `event` as sent (idempotently) and returns whether *this*
    call is the one that should actually fire the ping -- False if it was
    already marked sent before (by this or another process). Not
    perfectly race-free across two processes racing at the exact same
    instant (a plain read-modify-write, no file lock), but the
    consequence of losing that race is at most one duplicate anonymous
    lifecycle ping -- never a correctness or privacy problem, so a full
    lock isn't worth the complexity here."""
    state = load_state(app_data_dir)
    if event in state.sent_events:
        return False
    state.sent_events[event] = ts
    save_state(app_data_dir, state)
    return True


def mark_heartbeat_sent_if_due(
    app_data_dir: Path, *, now: datetime, min_interval: timedelta = HEARTBEAT_MIN_INTERVAL
) -> bool:
    """Same idempotency shape as `mark_event_sent_if_new`, but time-based
    instead of once-ever: returns whether *this* call should actually send
    a heartbeat -- True the first time ever, or whenever at least
    `min_interval` has elapsed since the last one actually sent. Not
    perfectly race-free across two processes (same plain read-modify-write
    caveat as `mark_event_sent_if_new`), and the same consequence: at most
    one duplicate heartbeat, never a correctness problem."""
    state = load_state(app_data_dir)
    if state.last_heartbeat_at is not None:
        last = datetime.fromisoformat(state.last_heartbeat_at)
        if now - last < min_interval:
            return False
    state.last_heartbeat_at = now.isoformat()
    save_state(app_data_dir, state)
    return True
