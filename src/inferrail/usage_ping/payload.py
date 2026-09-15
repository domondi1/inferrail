"""The exact, fixed payload shape for the opt-in usage ping -- the one
place that decides what leaves the machine when the ping is on. See
`docs/privacy/usage-ping.md` for the same shape in plain language, and
`inferrail telemetry preview` for a way to see it without trusting either
document.

Deliberately a `TypedDict`-shaped plain `dict`, not reusing any existing
receipt/telemetry model: those carry prompts, costs, work_ids, and other
fields that must never end up here by accident (e.g. from a future
field added to `InferenceReceipt` and someone reusing that model here
without noticing what else it carries).
"""

from __future__ import annotations

import platform
import sys
from datetime import UTC, datetime

from inferrail import __version__
from inferrail.usage_ping.state import KNOWN_EVENTS


def _os_name() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return platform.system().lower() or "unknown"


def build_payload(event: str, install_id: str) -> dict[str, str]:
    """The complete request body for one usage-ping event. Exhaustive --
    every field this function can ever produce is listed here, and
    nothing else is ever added downstream before it's sent."""
    if event not in KNOWN_EVENTS:
        raise ValueError(f"unknown usage-ping event {event!r}; known: {KNOWN_EVENTS}")
    return {
        "install_id": install_id,
        "event": event,
        "os": _os_name(),
        "inferrail_version": __version__,
        "ts": datetime.now(UTC).isoformat(),
    }
