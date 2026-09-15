"""The mutable, runtime on/off state for the usage ping, plus per-event
"already sent" bookkeeping -- kept separate from `inferrail.yaml` for the
same reason `BudgetStore` is separate from config (docs/adr/0015): a
config file is loaded once at startup, but the dashboard's Settings
toggle (and `inferrail telemetry enable|disable`) need to flip this live,
from a possibly different process than the one running
`inferrail serve --app-mode`.

`UsagePingConfig.enabled` (inferrail.yaml) only seeds this file's initial
value the first time it's created; after that, this file is the one
source of truth for whether the ping is on. `endpoint` is deliberately
*not* stored or settable here -- it stays operator/config-controlled only
(see `docs/adr/0019-opt-in-usage-ping.md`'s "why the dashboard can't set
the endpoint").
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel

_STATE_FILENAME = "usage-ping-state.json"

KNOWN_EVENTS = ("first_run", "tool_connected", "first_receipt", "budget_created")


class UsagePingState(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = False
    # event name -> ISO timestamp it was first sent, or absent if never sent.
    sent_events: dict[str, str] = {}


def _state_path(app_data_dir: Path) -> Path:
    return app_data_dir / _STATE_FILENAME


def load_state(app_data_dir: Path, *, default_enabled: bool = False) -> UsagePingState:
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
