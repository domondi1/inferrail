"""The per-user, per-OS application-data directory used only by
`inferrail serve --app-mode` (see docs/adr/0016-local-control-api.md).

Deliberately stdlib-only — no `platformdirs`/`appdirs` dependency —
matching this project's minimal dependency footprint (see
`pyproject.toml`). A normal `inferrail serve` (no `--app-mode`) never
calls this: its receipts/telemetry/budgets paths stay exactly what
`inferrail.yaml` (or the cwd-relative defaults) say, unaffected by
whether this module even exists.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_APP_NAME = "inferrail"


def app_data_dir() -> Path:
    """The OS-conventional per-user data directory for this app:

    - macOS: ``~/Library/Application Support/inferrail``
    - Windows: ``%APPDATA%\\inferrail`` (falling back to
      ``~/AppData/Roaming/inferrail`` if the env var is somehow unset)
    - Linux/other: ``$XDG_DATA_HOME/inferrail``, defaulting to
      ``~/.local/share/inferrail``

    Does not create the directory — callers that need it to exist
    (`ensure_app_data_dir`) do that explicitly, so merely importing or
    calling this function never touches the filesystem.
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / _APP_NAME


def ensure_app_data_dir() -> Path:
    """`app_data_dir()`, created (including parents) if it doesn't
    already exist. Safe to call repeatedly."""
    path = app_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path
