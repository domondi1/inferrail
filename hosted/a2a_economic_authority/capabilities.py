"""Inferrail Economic Authority — capability-token authorization.

High-entropy opaque bearer tokens, matching `docs/adr` conventions already
used elsewhere in this repo for durable, hash-verified state: the plaintext
token is generated with `secrets.token_urlsafe` (a CSPRNG), returned to the
minting caller exactly once, and never persisted anywhere -- only its SHA-256
hash, scopes, expiry, delegation binding, and revocation state are stored.
Verifying a presented token means hashing it and comparing hashes; there is
no way to recover a plaintext token from this store, by design.

This module knows nothing about A2A, HTTP, or economic amounts -- like
`core.py`, it is transport-independent. The executor (`executor.py`) is
responsible for extracting a bearer token from the HTTP `Authorization`
header and calling `CapabilityStore.authorize(...)` before touching any
economic state.

Six scopes cover every direct operation this service exposes:
`read`, `reserve`, `grant`, `consume`, `settle`, `revoke`.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCOPES = frozenset({"read", "reserve", "grant", "consume", "settle", "revoke"})

# 256 bits of entropy per token -- comfortably beyond brute-force range.
_TOKEN_ENTROPY_BYTES = 32
_DEFAULT_TTL_SECONDS = 3600
_DEFAULT_CLAIM_TTL_SECONDS = 300


class CapabilityError(Exception):
    """Base class for every reason a presented credential is rejected.

    Each subclass is a distinct, reportable failure reason -- callers must
    never collapse these into a single generic "unauthorized" without at
    least logging which one occurred (never logging the token itself).
    """


class MissingCredential(CapabilityError):
    pass


class InvalidCredential(CapabilityError):
    pass


class ExpiredCredential(CapabilityError):
    pass


class RevokedCredential(CapabilityError):
    pass


class InsufficientScope(CapabilityError):
    pass


class WrongDelegation(CapabilityError):
    pass


@dataclass(frozen=True)
class CapabilityInfo:
    token_id: str
    delegation_id: str
    scopes: frozenset[str]
    expires_at: str
    created_at: str


def _now() -> datetime:
    return datetime.now(UTC)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS capability_tokens (
    token_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    delegation_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_capability_tokens_delegation
    ON capability_tokens(delegation_id);
"""


class CapabilityStore:
    """SQLite-backed capability-token ledger. Persists hashes only.

    Durable and crash-safe by the same mechanism as
    `core.EconomicAuthorityStore`: every mutation runs inside a
    `BEGIN IMMEDIATE` transaction against the same kind of on-disk SQLite
    file, so revocation state survives a process restart exactly like
    economic state does.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def issue(
        self,
        delegation_id: str,
        scopes: frozenset[str] | set[str],
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> tuple[str, str]:
        """Mints a new capability. Returns `(token_id, plaintext_token)`.

        The plaintext value is generated here, returned to the immediate
        caller, and then never touched again by this store -- only its hash
        is written to disk. Delivering the plaintext onward (to an HTTP
        response, never to A2A message/task content) is the caller's job.
        """
        bad_scopes = set(scopes) - SCOPES
        if bad_scopes:
            raise ValueError(f"unknown scopes: {sorted(bad_scopes)}")
        if not scopes:
            raise ValueError("a capability must carry at least one scope")
        token_id = secrets.token_hex(16)
        plaintext = secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
        token_hash = _hash_token(plaintext)
        now = _now()
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO capability_tokens "
                "(token_id, token_hash, delegation_id, scopes, expires_at, revoked, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (
                    token_id,
                    token_hash,
                    delegation_id,
                    ",".join(sorted(scopes)),
                    expires_at,
                    now.isoformat(),
                ),
            )
        return token_id, plaintext

    def authorize(
        self, token: str | None, delegation_id: str, required_scope: str
    ) -> CapabilityInfo:
        """Verifies a bearer token grants `required_scope` on `delegation_id`.

        Raises a specific `CapabilityError` subclass on any failure and
        never returns a partial result -- callers must reject the request
        before touching economic state whenever this raises.
        """
        if required_scope not in SCOPES:
            raise ValueError(f"unknown scope: {required_scope!r}")
        if not token:
            raise MissingCredential("no bearer credential presented")
        row = self._lookup(token)
        if row is None:
            raise InvalidCredential("credential does not match any issued capability")
        if row["revoked"]:
            raise RevokedCredential("credential has been revoked")
        if datetime.fromisoformat(row["expires_at"]) <= _now():
            raise ExpiredCredential("credential has expired")
        if row["delegation_id"] != delegation_id:
            raise WrongDelegation("credential is not scoped to this delegation")
        scopes = frozenset(row["scopes"].split(","))
        if required_scope not in scopes:
            raise InsufficientScope(f"credential lacks required scope {required_scope!r}")
        return CapabilityInfo(
            token_id=row["token_id"],
            delegation_id=row["delegation_id"],
            scopes=scopes,
            expires_at=row["expires_at"],
            created_at=row["created_at"],
        )

    def live_scopes(self, token: str | None) -> frozenset[str] | None:
        """Returns a token's current scope set, or None if it does not exist,
        is revoked, or is expired. Used only to bound a newly-minted child
        capability's scopes to a subset of the issuing caller's own scopes
        -- never to authorize an operation by itself."""
        if not token:
            return None
        row = self._lookup(token)
        if row is None or row["revoked"]:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= _now():
            return None
        return frozenset(row["scopes"].split(","))

    def _lookup(self, token: str) -> sqlite3.Row | None:
        token_hash = _hash_token(token)
        with self._transaction() as conn:
            return conn.execute(
                "SELECT * FROM capability_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()

    def revoke_for_delegations(self, delegation_ids: list[str]) -> int:
        """Revokes every live token scoped to any of the given delegation_ids.

        This is the mechanism a full delegation-tree revoke uses once the
        caller (the executor, using `core.py`'s parent/child links) has
        computed which delegation_ids are in the subtree being revoked.
        """
        if not delegation_ids:
            return 0
        with self._transaction() as conn:
            placeholders = ",".join("?" for _ in delegation_ids)
            cur = conn.execute(
                f"UPDATE capability_tokens SET revoked = 1 "  # noqa: S608 -- placeholders only, no interpolated values
                f"WHERE delegation_id IN ({placeholders}) AND revoked = 0",
                delegation_ids,
            )
            return cur.rowcount


class InMemoryCredentialHandoff:
    """Non-persistent, single-use handoff for a freshly minted child token.

    Exists specifically because the installed A2A SDK's JSON-RPC transport
    (see `docs/adr` note in `server.py`) gives an `AgentExecutor` no channel
    to influence the outbound HTTP response other than A2A Task/Message
    content, which is durably persisted in `TaskStore` and must never carry
    a credential. This buffer lives only in this process's memory: it is
    never written to disk, is consumed exactly once, and is bound to the
    same bearer credential that triggered its creation -- so even a party
    that learns the (non-secret) claim_id from Task history cannot redeem
    it without also holding the original caller's own bearer token.

    Not durable across a process restart by design: an unclaimed handoff is
    lost on restart, same as an unclaimed one-time code from any other
    system would be. The economic effect of the `reserve` that created it
    is unaffected -- that state lives in `core.EconomicAuthorityStore`.
    """

    def __init__(self, ttl_seconds: int = _DEFAULT_CLAIM_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._pending: dict[str, tuple[str, str, str, float]] = {}
        # claim_id -> (token_id, plaintext_token, issuer_token_hash, expires_at_monotonic)

    def create(self, token_id: str, plaintext_token: str, issuer_token: str) -> str:
        claim_id = secrets.token_urlsafe(24)
        expires_at = time.monotonic() + self._ttl_seconds
        self._pending[claim_id] = (token_id, plaintext_token, _hash_token(issuer_token), expires_at)
        return claim_id

    def redeem(self, claim_id: str, presented_token: str | None) -> str:
        """Returns the plaintext token for `claim_id`, consuming it only on
        success. A wrong or missing credential leaves the claim intact --
        so a mistyped retry by the legitimate holder still works -- but an
        expired or successfully-redeemed claim is removed and can never be
        used again.

        Raises `MissingCredential`/`InvalidCredential`/`ExpiredCredential`/
        `WrongDelegation` (reused here as "wrong presented credential") on
        any failure.
        """
        entry = self._pending.get(claim_id)
        if entry is None:
            raise InvalidCredential("unknown or already-redeemed claim")
        _token_id, plaintext_token, issuer_hash, expires_at = entry
        if time.monotonic() > expires_at:
            del self._pending[claim_id]
            raise ExpiredCredential("claim has expired")
        if not presented_token:
            raise MissingCredential("no bearer credential presented")
        if _hash_token(presented_token) != issuer_hash:
            raise WrongDelegation("claim was not issued to this credential")
        del self._pending[claim_id]
        return plaintext_token
