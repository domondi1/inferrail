"""Inferrail Economic Authority — test-only root bootstrap (Phase B).

Provisions the initial root delegation and its root capability token
*before* a server process starts, by writing directly into the same SQLite
files the server will open. This is deliberately not an HTTP endpoint --
there is no unauthenticated public root-creation route anywhere in
`server.py`. Phase C is what adds a paid `POST /sessions` path; until then,
the only way a root delegation and its capability come into existence is
this function, called directly from test setup code in the same Python
process, never over the network.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

_HOSTED_DIR = Path(__file__).resolve().parent
if str(_HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(_HOSTED_DIR))

from capabilities import SCOPES, CapabilityStore  # noqa: E402
from core import EconomicAuthorityStore  # noqa: E402


def bootstrap_root(
    *,
    db_path: str | Path,
    capability_db_path: str | Path,
    delegation_id: str = "root",
    agent_id: str = "root-owner",
    envelope_usd: Decimal | str = "1.00",
    scopes: frozenset[str] = SCOPES,
    ttl_seconds: int = 3600,
) -> tuple[str, str]:
    """Creates the root delegation and mints its root capability token.

    Returns `(delegation_id, plaintext_root_token)`. The plaintext token is
    returned exactly once, here, in-process -- it is never written to disk
    (only its hash is, inside `capability_db_path`) and never printed or
    logged by this function.
    """
    core_store = EconomicAuthorityStore(db_path)
    core_store.create_root("bootstrap:root", delegation_id, agent_id, Decimal(envelope_usd))

    capability_store = CapabilityStore(capability_db_path)
    _token_id, plaintext = capability_store.issue(delegation_id, scopes, ttl_seconds=ttl_seconds)
    return delegation_id, plaintext
