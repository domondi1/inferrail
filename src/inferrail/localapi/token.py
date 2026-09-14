"""The per-install bearer token that guards the local control API (see
docs/adr/0016-local-control-api.md).

Deliberately separate from `INFERRAIL_GATEWAY_TOKEN`
(`gateway/routes.py`): that one is optional and guards the *inference*
routes (`/v1/chat/completions`, `/v1/messages`); this one is mandatory
whenever `--app-mode` is used and guards routes that read back
receipts/budgets/work data — a meaningfully different exposure (no
inference cost is incurred by hitting these routes, but a caller's own
local economic history is readable through them).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

_TOKEN_BYTES = 32


def ensure_local_api_token(path: str | Path) -> str:
    """Reads the token at `path`, generating and persisting a new one on
    first use. The file is written with owner-only permissions (`0600`)
    at creation time — best-effort on Windows, where POSIX file modes
    aren't meaningful; NTFS ACLs already default to the owning user
    there. Never regenerates an existing token: the whole point is a
    *stable* per-install secret a caller (or the future desktop app)
    saves once and reuses."""
    token_path = Path(path)
    if token_path.exists():
        return token_path.read_text(encoding="utf-8").strip()

    token = secrets.token_urlsafe(_TOKEN_BYTES)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Lost a race with another process creating it first — read back
        # whatever it wrote rather than raising or silently overwriting.
        return token_path.read_text(encoding="utf-8").strip()
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    return token
