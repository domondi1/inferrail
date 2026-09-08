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
import threading
import time
from collections.abc import Callable, Iterator
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


class WrongAuthorizer(CapabilityError):
    """The presented credential is itself valid (right delegation, right
    scope, not expired or revoked) but is not the exact credential that
    originally authorized the reservation this claim belongs to."""


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
            row: sqlite3.Row | None = conn.execute(
                "SELECT * FROM capability_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            return row

    def token_id_for(self, token: str | None) -> str | None:
        """Returns the (non-secret) `token_id` for a presented plaintext
        token, regardless of whether it is revoked or expired -- unlike
        `authorize()`, this never raises. Used only to answer "is this the
        SAME credential as some other, already-known token_id", never to
        authorize anything by itself: an expired or revoked token still
        has a token_id, and a caller comparing token_ids must separately
        decide what an expired/revoked match means for their use case
        (see `InMemoryCredentialHandoff.redeem`)."""
        if not token:
            return None
        row = self._lookup(token)
        return None if row is None else str(row["token_id"])

    def scopes_issued_for(self, delegation_id: str) -> frozenset[str] | None:
        """Returns the scope set of the FIRST-ever capability issued for
        `delegation_id` (by issuance order), or None if none was ever
        issued. Used only to detect a reservation retry naming
        different `child_scopes` than what was originally minted -- an
        explicit conflict (see `executor.py`'s reserve-retry handling) --
        never to authorize anything by itself. Returns the historical
        record regardless of whether that original token has since been
        revoked or expired."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT scopes FROM capability_tokens WHERE delegation_id = ? "
                "ORDER BY created_at ASC LIMIT 1",
                (delegation_id,),
            ).fetchone()
        return None if row is None else frozenset(row["scopes"].split(","))

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


@dataclass(frozen=True)
class _PendingClaim:
    token_id: str
    plaintext_token: str
    issuer_delegation_id: str
    issuer_required_scope: str
    authorizing_token_id: str
    child_delegation_id: str
    expires_at_monotonic: float


class InMemoryCredentialHandoff:
    """Non-persistent, single-use handoff for a freshly minted child token.

    Exists specifically because the installed A2A SDK's JSON-RPC transport
    (see the module docstring in `server.py`) gives an `AgentExecutor` no
    channel to influence the outbound HTTP response other than A2A
    Task/Message content, which is durably persisted in `TaskStore` and
    must never carry a credential. This buffer lives only in this
    process's memory: it is never written to disk, and is consumed exactly
    once.

    Redemption is bound to the EXACT credential that originally authorized
    the reservation (`authorizing_token_id`, the non-secret `token_id` of
    that credential -- never the plaintext token itself). At redemption
    time (`redeem`), the presented bearer token is revalidated in full
    against the live `CapabilityStore` (existence, expiry, revocation,
    delegation binding, scope) via the same `authorize()` every other
    operation uses, AND its resolved `token_id` must equal
    `authorizing_token_id` exactly. This means: (1) a credential that has
    since expired or been revoked cannot redeem a claim, including the
    original authorizer itself; (2) a grant-only credential can supply the
    missing authority for a parked reservation but can never redeem it,
    since it never becomes the authorizer; and (3) a DIFFERENT credential
    that happens to also hold `reserve` scope on the same parent -- even a
    perfectly legitimate one for other purposes -- cannot redeem a
    reservation it did not itself authorize, closing the "guess an
    existing child_id, present any reserve-scoped token for the parent"
    vector.

    Purging is scoped precisely to protect the rightful claimant:
    `redeem` determines whether the PRESENTED credential's token_id is
    actually the authorizer BEFORE deciding what an expired/revoked
    failure means. An unrelated token (wrong delegation, wrong scope, or
    simply a different credential entirely) that happens to be expired or
    revoked proves nothing about the rightful authorizer's credential, so
    presenting one never destroys someone else's still-redeemable claim --
    only the true authorizer's own expiry/revocation is treated as
    terminal and purges the claim. Tree revocation reaching the specific
    child delegation this claim is for is also terminal and purges it
    (via the optional `is_target_revoked` callback in `redeem`, checked
    against `core.EconomicAuthorityStore.is_revocation_in_progress` in
    practice -- kept as a callback rather than a hard import so this
    module stays transport/core-independent).

    Not durable across a process restart by design: an unclaimed handoff is
    lost on restart, same as an unclaimed one-time code from any other
    system would be. The economic effect of the `reserve` that created it
    is unaffected -- that state lives in `core.EconomicAuthorityStore`.

    Thread-safe via a plain `threading.Lock`: `redeem` never awaits between
    reading and mutating `_pending`, so concurrent redemption attempts for
    the same claim_id are strictly serialized and at most one can succeed.
    This buffer is process-local by design (see `server.py`'s
    single-process requirement) -- the lock only needs to work within one
    process, not across a future multi-worker deployment.
    """

    def __init__(self, ttl_seconds: int = _DEFAULT_CLAIM_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._pending: dict[str, _PendingClaim] = {}
        self._lock = threading.Lock()

    def create(
        self,
        token_id: str,
        plaintext_token: str,
        issuer_delegation_id: str,
        issuer_required_scope: str,
        *,
        authorizing_token_id: str,
        child_delegation_id: str,
    ) -> str:
        """`authorizing_token_id` is the non-secret `token_id` of the
        credential that authorized the reserve this claim belongs to
        (from the `CapabilityInfo` that call's own `authorize()` returned)
        -- never a plaintext token. `child_delegation_id` is the
        delegation the newly-minted credential is scoped to, used only to
        check whether that specific delegation's tree has since begun
        revocation."""
        claim_id = secrets.token_urlsafe(24)
        expires_at = time.monotonic() + self._ttl_seconds
        with self._lock:
            self._pending[claim_id] = _PendingClaim(
                token_id=token_id,
                plaintext_token=plaintext_token,
                issuer_delegation_id=issuer_delegation_id,
                issuer_required_scope=issuer_required_scope,
                authorizing_token_id=authorizing_token_id,
                child_delegation_id=child_delegation_id,
                expires_at_monotonic=expires_at,
            )
        return claim_id

    def redeem(
        self,
        capability_store: CapabilityStore,
        claim_id: str,
        presented_token: str | None,
        *,
        is_target_revoked: Callable[[str], bool] | None = None,
    ) -> str:
        """Returns the plaintext token for `claim_id`, consuming it only on
        success.

        `is_target_revoked`, if given, is called with the claim's
        `child_delegation_id`; if it returns True the claim is treated as
        terminally dead (purged) even though the presented credential
        itself might still be perfectly valid -- the delegation tree it
        would grant access to no longer has usable authority. Kept
        optional and duck-typed so this module never has to import
        `core.py` directly.

        Raises the specific `CapabilityError` subclass `authorize()`
        raised for the presented credential, `WrongAuthorizer` if the
        credential is valid but is not the one that authorized this
        reservation, or `InvalidCredential`/`ExpiredCredential` for a
        claim_id problem itself.
        """
        with self._lock:
            entry = self._pending.get(claim_id)
            if entry is None:
                raise InvalidCredential("unknown or already-redeemed claim")
            if time.monotonic() > entry.expires_at_monotonic:
                del self._pending[claim_id]
                raise ExpiredCredential("claim has expired")

            # Resolve the presented credential's own token_id BEFORE
            # calling authorize() (which may raise for a revoked/expired
            # token) so we can tell whether a subsequent revoked/expired
            # failure is actually about the rightful authorizer, or about
            # some unrelated credential that proves nothing about it.
            presented_token_id = capability_store.token_id_for(presented_token)
            is_the_authorizer = (
                presented_token_id is not None and presented_token_id == entry.authorizing_token_id
            )

            try:
                info = capability_store.authorize(
                    presented_token, entry.issuer_delegation_id, entry.issuer_required_scope
                )
            except (RevokedCredential, ExpiredCredential):
                if is_the_authorizer:
                    # The rightful authorizer is now permanently gone --
                    # this claim can never be redeemed by anyone, so purge
                    # it rather than leave a dead entry (and a live
                    # plaintext token) sitting in memory.
                    del self._pending[claim_id]
                # An unrelated credential's own expiry/revocation proves
                # nothing about the rightful authorizer -- the claim
                # survives for them to redeem later.
                raise
            except CapabilityError:
                # Wrong/missing/insufficiently-scoped credential: the
                # claim itself may still be legitimately redeemable by its
                # rightful holder, so it survives this failed attempt.
                raise

            if info.token_id != entry.authorizing_token_id:
                # A currently-valid credential, but not the one that
                # authorized this reservation -- e.g. a different
                # reserve-scoped credential for the same parent. The
                # claim survives; only the true authorizer can redeem it.
                raise WrongAuthorizer(
                    "credential is valid but did not authorize this reservation"
                )

            if is_target_revoked is not None and is_target_revoked(entry.child_delegation_id):
                del self._pending[claim_id]
                raise RevokedCredential(
                    "the delegation this credential would grant access to is undergoing revocation"
                )

            del self._pending[claim_id]
            return entry.plaintext_token

    def purge_for_delegations(self, delegation_ids: list[str]) -> int:
        """Removes every outstanding claim whose issuer OR child target
        belongs to any of the given delegation_ids. Called when a
        delegation tree is revoked, so an unclaimed child credential from a
        reservation whose authority no longer exists cannot be redeemed
        just because it hadn't expired yet.

        Both sides matter: a claim is purged if the delegation that
        authorized it (`issuer_delegation_id`) is revoked -- the reason it
        was originally checked -- but also if the delegation the claim
        would *grant access to* (`child_delegation_id`) is revoked, even
        when its issuer lies outside the revoked subtree (e.g. the issuer
        is an ancestor). Otherwise revoking a child before its own credential
        is claimed -- while the reservation that authorized it lives on a
        surviving ancestor -- would leave a live, redeemable claim for a
        delegation that no longer exists."""
        if not delegation_ids:
            return 0
        wanted = set(delegation_ids)
        with self._lock:
            purge = [
                cid
                for cid, entry in self._pending.items()
                if entry.issuer_delegation_id in wanted or entry.child_delegation_id in wanted
            ]
            for claim_id in purge:
                del self._pending[claim_id]
            return len(purge)
