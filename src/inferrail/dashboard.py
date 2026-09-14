"""Locating the built dashboard SPA (`app/dist`) to mount under
`/dashboard` when `inferrail serve --app-mode` runs — see
docs/adr/0017-dashboard-in-app-directory.md.

Deliberately separate from `gateway/app.py`'s other wiring: this is the
one place a future packaging unit needs to change to make `pip install
inferrail` alone ship a working dashboard (see that ADR's "Known gap").
"""

from __future__ import annotations

import os
from pathlib import Path


def find_dashboard_dist() -> Path | None:
    """Returns the directory containing the built dashboard's
    `index.html`, or `None` if no build is reachable. Checked in order:

    1. `INFERRAIL_DASHBOARD_DIR` env var, if set — an explicit override,
       useful for development or a non-standard install layout.
    2. `dashboard_static/` bundled inside this installed package —
       reserved for a future packaging unit that copies `app/dist` there
       at wheel-build time; not yet populated, so this is always a miss
       today.
    3. An `app/dist` directory found by walking up from this file's own
       location — what actually resolves today when running from a git
       checkout with the dashboard already built (`cd app && npm run
       build`).

    Never raises: a missing or unbuilt dashboard is a normal, expected
    state (see ADR-0017's "Consequences"), not an error.
    """
    override = os.environ.get("INFERRAIL_DASHBOARD_DIR")
    if override:
        candidate = Path(override)
        return candidate if (candidate / "index.html").is_file() else None

    here = Path(__file__).resolve()

    bundled = here.parent / "dashboard_static"
    if (bundled / "index.html").is_file():
        return bundled

    for parent in here.parents:
        candidate = parent / "app" / "dist"
        if (candidate / "index.html").is_file():
            return candidate

    return None
