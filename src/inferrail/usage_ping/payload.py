"""The exact, fixed payload shape for the usage ping -- the one place that
decides what leaves the machine when the ping is on. See
`docs/privacy/usage-ping.md` for the same shape in plain language, and
`inferrail telemetry preview` for a way to see it without trusting either
document.

Deliberately a `TypedDict`-shaped plain `dict`, not reusing any existing
receipt/telemetry model: those carry prompts, costs, work_ids, and other
fields that must never end up here by accident (e.g. from a future
field added to `InferenceReceipt` and someone reusing that model here
without noticing what else it carries).

Field set and event names match
docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md
exactly -- no `ts` field (the collector stamps `seen_at` itself on
receipt, so the client never needs to, and a client clock can't be
trusted anyway), `version` (not `inferrail_version`), and a new
`python_version` (major.minor only, never a full patch/build string that
could narrow fingerprinting).
"""

from __future__ import annotations

import platform
import sys

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


def _python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def build_payload(event: str, install_id: str) -> dict[str, str]:
    """The complete request body for one usage-ping event. Exhaustive --
    every field this function can ever produce is listed here, and
    nothing else is ever added downstream before it's sent."""
    if event not in KNOWN_EVENTS:
        raise ValueError(f"unknown usage-ping event {event!r}; known: {KNOWN_EVENTS}")
    return {
        "install_id": install_id,
        "event": event,
        "version": __version__,
        "os": _os_name(),
        "python_version": _python_version(),
    }
