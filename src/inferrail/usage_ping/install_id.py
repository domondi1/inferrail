"""A random, local, per-install identifier for the opt-in usage ping.

Deliberately not derived from any hardware/network identifier (MAC
address, disk serial, hostname) -- it exists only to let a receiving
collector de-duplicate repeat events from the same install, never to be
traceable back to a specific machine or person. Same race-safe
create-once-then-reuse pattern as `localapi.token.ensure_local_api_token`.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

_ID_FILENAME = "usage-ping-install-id"


def ensure_install_id(app_data_dir: Path) -> str:
    """Reads the install id under `app_data_dir`, generating and
    persisting a new one on first use. Never regenerates an existing id."""
    id_path = app_data_dir / _ID_FILENAME
    if id_path.exists():
        return id_path.read_text(encoding="utf-8").strip()

    install_id = uuid.uuid4().hex
    app_data_dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(id_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        # Lost a race with another process creating it first.
        return id_path.read_text(encoding="utf-8").strip()
    try:
        os.write(fd, install_id.encode("utf-8"))
    finally:
        os.close(fd)
    return install_id
