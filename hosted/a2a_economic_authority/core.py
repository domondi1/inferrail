"""Inferrail Economic Authority — durable economic-authority core.

Given a caller-declared spending ceiling for a unit of work (a
"delegation"), this module tracks how much of that ceiling has been
reserved for sub-work, actually consumed, and released — atomically,
durably, and honestly about what is known versus unknown.

This module is transport-independent: it knows nothing about A2A, HTTP,
or payment. A transport adapter (added separately) is responsible for
authenticating a caller, extracting a `delegation_id` from whatever
protocol it speaks, and calling this store. This module is the sole
authority for economic state. The delegated `authority_usd` ceiling is
caller-declared accounting/policy metadata -- Inferrail never holds,
transfers, or escrows the underlying money.

Core distinction this module exists to enforce:

    delegation_id  -- durable economic identity. Created once by whoever
                      opens the delegation. Stable across retries,
                      transport reconnects, and process restarts.

    event_id       -- idempotency key for one economic action attempt
                      (reserve/grant/consume/settle), scoped to the
                      delegation it names -- see "Idempotency boundary"
                      below. A duplicate delivery of the same attempt
                      (same delegation_id, same event_id, same canonical
                      request payload) reuses the same key and must not
                      mutate state twice. A genuinely new attempt (a real
                      retry with new information, or a second real
                      charge) uses a new event_id and may have a real
                      economic effect. Reusing a key with a *different*
                      payload is a caller bug or a conflicting reuse, and
                      is rejected explicitly (`EventConflict`) rather than
                      silently treated as either a safe retry or a
                      genuinely new event.

Idempotency boundary: event_ids are scoped to `(delegation_id, event_id)`,
never to `event_id` alone. Two unrelated delegations -- e.g. belonging to
two independent, mutually untrusting agents -- choosing the same
caller-picked event_id string must never collide; each is tracked and
deduplicated entirely independently. `reserve`'s natural idempotency key
is the *child* `delegation_id` itself (a duplicate reserve for a
delegation_id that already exists is a no-op, provided its recorded
parent/agent/amount match what's being requested again -- otherwise the
reuse is a conflict, not a retry). Canonical payloads compare Decimal
amounts by normalized value (`_canonical_decimal_str`), not by the
caller's original string form, so "1.0" and "1.00" are recognized as the
same amount and never produce a false conflict.

Concurrency and crash safety come from SQLite: every mutating operation
runs inside a single `BEGIN IMMEDIATE` transaction, which takes SQLite's
write lock before reading state, so two callers racing to reserve the
same headroom serialize correctly instead of both observing the
pre-reservation balance. Commits are durable, so a process killed
immediately after a commit still has that effect on restart, and a
process killed before commit has none of it -- there is no partial state
to reconcile.

Revocation race safety: `mark_revocation_started` durably records, on the
delegation being revoked, that it (and therefore every current and future
descendant) is being torn down. `reserve`, `grant`, and `consume` each
check this flag across the *entire* ancestor chain of the delegation they
would mutate, inside their own atomic transaction, before doing anything.
Because SQLite's `BEGIN IMMEDIATE` serializes all writers against one
database file, there is no interleaving in which any of these calls can
commit a new effect after the flag has been set on any ancestor, and no
interleaving in which one that committed *before* the flag was set can be
invisible to a subtree scan performed *after* it -- one happens strictly
before the other. This closes the snapshot-then-settle race a caller
(e.g. an A2A executor) would otherwise have when tearing down a whole
tree: mark first, then snapshot and settle, and nothing can escape. Every
step of that teardown (settle each descendant, revoke each capability,
purge each outstanding claim) is independently idempotent, so a caller
that crashes mid-teardown can simply retry the same revoke and it safely
resumes and finishes.

Grant conservation: a grant against a delegation with a parent is treated
exactly like an additional reservation carved out of that parent -- it
increases the parent's `child_reserved_usd` by the same amount it
increases the child's `authority_usd`, atomically, and is rejected if the
parent lacks that much certain (non-unknown) headroom. This is what keeps
`settle`'s existing parent-reconciliation formula correct in the presence
of grants; without it, a child could hold more authority than its parent
ever actually set aside, and settling that child could drive the parent's
own accounting negative. A grant against a *root* delegation (no parent)
remains a pure top-up -- root authority is external policy/administrative
top-up, not backed by anything in this store, and is never confused with
payment (Phase B adds no payment of any kind).

Money is stored as canonical Decimal strings and never as float. Unknown
cost is tracked explicitly (a count, never folded into a fabricated
total) -- the same "unknown stays unknown" discipline already shipped for
`inferrail.receipts.InferenceReceipt` and `hosted/work_economics`. NaN,
Infinity, and non-finite Decimal inputs are rejected before they can ever
reach a stored balance.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

Kind = Literal[
    "root", "reservation", "authority_grant", "consumption", "settlement", "revocation_mark"
]
ReserveOutcome = Literal["created", "already_exists", "rejected"]

# Schema version this module's code expects. Bumped whenever SCHEMA or the
# shape of a stored row changes in a way that requires migrating an
# on-disk database created by an earlier version -- see `_migrate`.
CURRENT_SCHEMA_VERSION = 2

MAX_IDENTIFIER_LENGTH = 256
# 1 trillion -- a sanity ceiling, not a real cap.
MAX_REASONABLE_AMOUNT_USD = Decimal("1000000000000")


class EventConflict(ValueError):
    """A caller reused a `(delegation_id, event_id)` pair with a request
    that does not match what was originally recorded under that key.

    This is deliberately a `ValueError` subclass: transport layers that
    already catch `ValueError` for malformed-input handling (see
    `executor.py`) get a safe default (a clean rejection, no retry, no
    silent success) without needing a bespoke except clause, while still
    being able to catch `EventConflict` specifically when they want a more
    precise error message.
    """


def validate_identifier(name: str, value: str) -> None:
    """Rejects an empty, oversized, or non-string identifier. Used for
    delegation_id/parent_id/agent_id/event_id before they ever reach a
    query. Not a character-set allowlist -- these values are always used
    in parameterized queries, so there is no injection risk -- just a
    sanity bound against pathological input."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{name} must be at most {MAX_IDENTIFIER_LENGTH} characters")


def validate_amount(value: Decimal, *, name: str = "amount") -> None:
    """Rejects NaN, Infinity, negative-beyond-repair, or absurdly large
    amounts before they can reach stored state or arithmetic. Callers
    still separately decide whether negative is meaningful for their
    specific operation; this only rules out non-finite and pathological
    magnitudes."""
    if not value.is_finite():
        raise ValueError(f"{name} must be a finite number (no NaN/Infinity)")
    if abs(value) > MAX_REASONABLE_AMOUNT_USD:
        raise ValueError(f"{name} exceeds the maximum reasonable amount")


@dataclass(frozen=True)
class DelegationState:
    delegation_id: str
    parent_delegation_id: str | None
    agent_id: str
    authority_usd: Decimal
    consumed_usd: Decimal
    child_reserved_usd: Decimal
    released_usd: Decimal
    unknown_cost_count: int
    active: bool
    outcome: str | None
    revocation_started_at: str | None
    created_at: str
    updated_at: str

    @property
    def active_reservation_usd(self) -> Decimal:
        return self.authority_usd - self.consumed_usd - self.child_reserved_usd


@dataclass(frozen=True)
class InvariantResult:
    delegation_id: str
    satisfied_on_known_values: bool
    certainty: Literal["FULL", "PARTIAL"]
    label: str
    detail: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_decimal_str(value: Decimal) -> str:
    """Canonical string form of a Decimal amount for idempotency-key
    comparison: "1.0" and "1.00" (and "100" and "100.00") normalize to
    the same string, so they are never mistaken for a conflicting reuse
    of the same event_id. Always plain-point notation -- `Decimal.
    normalize()` alone can produce scientific notation (e.g. "100" ->
    "1E+2"), which `format(value, 'f')` avoids."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _canonical_json(payload: dict[str, Any]) -> str:
    normalized = {
        key: (_canonical_decimal_str(value) if isinstance(value, Decimal) else value)
        for key, value in payload.items()
    }
    return json.dumps(normalized, sort_keys=True, default=str)


def _row_to_state(row: sqlite3.Row) -> DelegationState:
    return DelegationState(
        delegation_id=row["delegation_id"],
        parent_delegation_id=row["parent_delegation_id"],
        agent_id=row["agent_id"],
        authority_usd=Decimal(row["authority_usd"]),
        consumed_usd=Decimal(row["consumed_usd"]),
        child_reserved_usd=Decimal(row["child_reserved_usd"]),
        released_usd=Decimal(row["released_usd"]),
        unknown_cost_count=row["unknown_cost_count"],
        active=bool(row["active"]),
        outcome=row["outcome"],
        revocation_started_at=row["revocation_started_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delegations (
    delegation_id TEXT PRIMARY KEY,
    parent_delegation_id TEXT,
    agent_id TEXT NOT NULL,
    authority_usd TEXT NOT NULL,
    consumed_usd TEXT NOT NULL DEFAULT '0',
    child_reserved_usd TEXT NOT NULL DEFAULT '0',
    released_usd TEXT NOT NULL DEFAULT '0',
    unknown_cost_count INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    outcome TEXT,
    revocation_started_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS economic_events (
    delegation_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    canonical_payload TEXT NOT NULL,
    amount_usd TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (delegation_id, event_id)
);
"""


class EconomicAuthorityStore:
    """SQLite-backed authoritative economic state for one deployment.

    One store instance may be opened by many processes against the same
    `db_path`; each mutating call opens its own short-lived connection so
    no connection is held open across a crash-injection boundary.

    Opening a database created by an earlier schema version runs
    `_migrate` automatically and atomically -- see that method's
    docstring for the exact migration this version performs.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)  # executescript manages its own transaction
            self._migrate(conn)
        finally:
            conn.close()

    def _schema_version(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            return 0
        return int(row["value"])

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Atomically upgrades an on-disk database to `CURRENT_SCHEMA_VERSION`.

        `CREATE TABLE IF NOT EXISTS` (already applied via `SCHEMA` before
        this runs) creates missing tables/columns for a brand-new
        database, but does nothing for a database that already has
        `delegations`/`economic_events` in an OLDER shape -- SQLite does
        not add missing columns to an existing table that way. This
        method detects that case and migrates it, entirely inside one
        `BEGIN IMMEDIATE` transaction: either every step succeeds and
        `schema_version` is recorded, or the whole transaction rolls back
        and the database is left exactly as it was -- there is no
        half-migrated state possible.

        Version 0 -> 1 (the pre-Phase-B-repair schema, identified by the
        presence of `delegations` but absence of `schema_meta`/
        `revocation_started_at`, or `economic_events` still keyed on a
        bare `event_id`):

        - Adds `delegations.revocation_started_at` (nullable; existing
          rows get NULL, i.e. "not undergoing revocation", which is
          correct -- nothing was mid-revocation under a schema that had
          no concept of it).
        - Rebuilds `economic_events` onto the new `(delegation_id,
          event_id)` composite primary key with a `canonical_payload`
          column. Existing event rows are preserved: each legacy row's
          `delegation_id`/`event_id`/`kind`/`amount_usd`/`status`/
          `created_at` carry over unchanged. For the five kinds the
          legacy schema ever produced (root/reservation/authority_grant/
          consumption/settlement), the canonical payload is reconstructed
          in EXACTLY the shape live code would produce today, by joining
          each event row against its delegation's CURRENT row (which is
          untouched by this migration and still has `agent_id`/
          `parent_delegation_id`) for the fields `economic_events` itself
          never stored -- so a genuine post-migration retry of a
          pre-migration event_id is still recognized as a safe retry, not
          a false conflict, and an actually-conflicting reuse is still
          caught. Any row of an unrecognized kind, or whose delegation no
          longer exists, gets a payload that can never coincidentally
          match a live-code-constructed one (so a later reuse of that key
          is treated as new rather than silently matched) -- conservative
          by construction, and irrelevant in practice since this public
          feature has no deployed database and therefore no real
          pre-migration traffic to ever replay.

        Because this public feature has no deployed database yet, this
        is the only migration this module needs to carry -- but the
        `schema_meta` version marker means a future schema change can add
        its own step the same way, unambiguously, without re-running this
        one.
        """
        current_version = self._schema_version(conn)
        if current_version >= CURRENT_SCHEMA_VERSION:
            return

        with self._transaction_on(conn):
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(delegations)").fetchall()
            }
            if "revocation_started_at" not in columns:
                conn.execute("ALTER TABLE delegations ADD COLUMN revocation_started_at TEXT")

            event_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(economic_events)").fetchall()
            }
            if "canonical_payload" not in event_columns or "delegation_id" not in event_columns:
                self._migrate_economic_events_table(conn)

            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(CURRENT_SCHEMA_VERSION),),
            )

    def _migrate_economic_events_table(self, conn: sqlite3.Connection) -> None:
        """Rebuilds `economic_events` from its legacy (pre-repair) shape,
        which had `event_id TEXT PRIMARY KEY` (global, not per-delegation)
        and no `canonical_payload` column, onto the current
        `(delegation_id, event_id)` composite-keyed shape -- preserving
        every existing row.
        """
        legacy_rows = conn.execute("SELECT * FROM economic_events").fetchall()
        legacy_columns = {row[1] for row in conn.execute("PRAGMA table_info(economic_events)")}

        conn.execute("ALTER TABLE economic_events RENAME TO economic_events_legacy")
        conn.execute(
            """
            CREATE TABLE economic_events (
                delegation_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                canonical_payload TEXT NOT NULL,
                amount_usd TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (delegation_id, event_id)
            )
            """
        )
        for row in legacy_rows:
            delegation_id = row["delegation_id"] if "delegation_id" in legacy_columns else None
            if delegation_id is None:
                # A legacy row with no delegation_id at all is not
                # reconstructable; conservatively skip rather than guess.
                continue
            raw_amount = row["amount_usd"] if "amount_usd" in legacy_columns else None
            amount_usd = None if raw_amount is None else Decimal(str(raw_amount))
            kind = row["kind"] if "kind" in legacy_columns else "unknown"
            status = row["status"] if "status" in legacy_columns else "accepted"
            delegation_row = self._get(conn, delegation_id)

            payload: dict[str, Any] | None = None
            if delegation_row is not None:
                if kind == "root":
                    payload = {
                        "kind": "root",
                        "delegation_id": delegation_id,
                        "agent_id": delegation_row["agent_id"],
                        "envelope_usd": (
                            amount_usd
                            if amount_usd is not None
                            else Decimal(delegation_row["authority_usd"])
                        ),
                    }
                elif kind == "reservation":
                    payload = {
                        "kind": "reservation",
                        "parent_id": delegation_row["parent_delegation_id"],
                        "delegation_id": delegation_id,
                        "agent_id": delegation_row["agent_id"],
                        "maximum_usd": (
                            amount_usd
                            if amount_usd is not None
                            else Decimal(delegation_row["authority_usd"])
                        ),
                    }
                elif kind == "authority_grant":
                    payload = {
                        "kind": "authority_grant",
                        "delegation_id": delegation_id,
                        "amount_usd": amount_usd,
                    }
                elif kind == "consumption":
                    payload = {
                        "kind": "consumption",
                        "delegation_id": delegation_id,
                        "amount_usd": amount_usd,
                    }
                elif kind == "settlement":
                    payload = {
                        "kind": "settlement",
                        "delegation_id": delegation_id,
                        "outcome": status,
                    }
            if payload is None:
                # Unrecognized kind, or the delegation this event belonged
                # to no longer exists: reconstruct a payload that can
                # never coincidentally match what live code would build
                # for a real replay, so a later reuse of this exact key is
                # treated as a genuinely new event rather than silently
                # matched (or silently misjudged as a conflict against a
                # guess). See this method's docstring.
                payload = {
                    "kind": kind,
                    "delegation_id": delegation_id,
                    "_unreconstructed_legacy_event_id": row["event_id"],
                }
            canonical_payload = _canonical_json(payload)
            conn.execute(
                "INSERT INTO economic_events "
                "(delegation_id, event_id, kind, canonical_payload, "
                "amount_usd, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    delegation_id,
                    row["event_id"],
                    kind,
                    canonical_payload,
                    raw_amount,
                    status,
                    row["created_at"] if "created_at" in legacy_columns else _now(),
                ),
            )
        conn.execute("DROP TABLE economic_events_legacy")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with self._transaction_on(conn):
                yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction_on(self, conn: sqlite3.Connection) -> Iterator[None]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def _get(self, conn: sqlite3.Connection, delegation_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM delegations WHERE delegation_id = ?", (delegation_id,)
        ).fetchone()
        return row

    def _is_revocation_in_progress_in_lineage(
        self, conn: sqlite3.Connection, delegation_id: str
    ) -> bool:
        """True if `delegation_id` or any of its ancestors, up to the root,
        has been marked by `mark_revocation_started`. Walked inside the
        caller's own transaction so the check is atomic with whatever
        mutation it's guarding."""
        current: str | None = delegation_id
        while current is not None:
            row = self._get(conn, current)
            if row is None:
                return False
            if row["revocation_started_at"] is not None:
                return True
            current = row["parent_delegation_id"]
        return False

    def is_revocation_in_progress(self, delegation_id: str) -> bool:
        """Public, read-only version of the ancestor-chain revocation
        check, for callers outside a mutation (e.g. capability-claim
        redemption) that need to fail closed once a tree's teardown has
        begun, even before settlement/token-revocation finish."""
        with self._transaction() as conn:
            return self._is_revocation_in_progress_in_lineage(conn, delegation_id)

    def _existing_event_payload(
        self, conn: sqlite3.Connection, delegation_id: str, event_id: str
    ) -> str | None:
        row = conn.execute(
            "SELECT canonical_payload FROM economic_events "
            "WHERE delegation_id = ? AND event_id = ?",
            (delegation_id, event_id),
        ).fetchone()
        return None if row is None else row["canonical_payload"]

    def _check_event(
        self,
        conn: sqlite3.Connection,
        delegation_id: str,
        event_id: str,
        canonical_payload: dict[str, Any],
    ) -> bool:
        """Returns True if `(delegation_id, event_id)` was already recorded
        with this exact canonical payload (a safe retry -- the caller
        should treat this as already-done and not mutate again). Returns
        False if it has never been seen. Raises `EventConflict` if it was
        recorded before with a *different* payload -- the same key reused
        for a materially different request, which must never be silently
        treated as either outcome.
        """
        existing = self._existing_event_payload(conn, delegation_id, event_id)
        if existing is None:
            return False
        new = _canonical_json(canonical_payload)
        if existing != new:
            raise EventConflict(
                f"event_id {event_id!r} was already used for delegation "
                f"{delegation_id!r} with a different request"
            )
        return True

    def _record_event(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        delegation_id: str,
        kind: Kind,
        canonical_payload: dict[str, Any],
        amount_usd: Decimal | None,
        status: str,
    ) -> None:
        amount = None if amount_usd is None else str(amount_usd)
        conn.execute(
            "INSERT INTO economic_events "
            "(delegation_id, event_id, kind, canonical_payload, amount_usd, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                delegation_id,
                event_id,
                kind,
                _canonical_json(canonical_payload),
                amount,
                status,
                _now(),
            ),
        )

    # -- creation -----------------------------------------------------

    def create_root(
        self, event_id: str, delegation_id: str, agent_id: str, envelope_usd: Decimal
    ) -> DelegationState:
        """Idempotent bootstrap of a root (parentless) delegation.

        A duplicate call naming the same `delegation_id` with the same
        `agent_id`/`envelope_usd` -- whatever `event_id` it arrives with --
        returns the existing row unchanged. A call naming an *existing*
        `delegation_id` with a *different* `agent_id` or `envelope_usd` is
        a conflicting reuse and raises `EventConflict` rather than quietly
        keeping the first value or silently re-minting. Root authority can
        only be created here, once, per delegation_id; it is never
        re-minted by replay.
        """
        validate_identifier("delegation_id", delegation_id)
        validate_identifier("agent_id", agent_id)
        validate_identifier("event_id", event_id)
        validate_amount(envelope_usd, name="envelope_usd")
        with self._transaction() as conn:
            existing = self._get(conn, delegation_id)
            if existing is not None:
                existing_state = _row_to_state(existing)
                if (
                    existing_state.agent_id != agent_id
                    or existing_state.authority_usd != envelope_usd
                ):
                    raise EventConflict(
                        f"delegation_id {delegation_id!r} already exists with a different "
                        f"agent_id/envelope_usd"
                    )
                return existing_state
            if envelope_usd < 0:
                raise ValueError("envelope must be non-negative")
            now = _now()
            conn.execute(
                "INSERT INTO delegations "
                "(delegation_id, parent_delegation_id, agent_id, authority_usd, "
                "consumed_usd, child_reserved_usd, released_usd, unknown_cost_count, "
                "active, outcome, revocation_started_at, created_at, updated_at) "
                "VALUES (?, NULL, ?, ?, '0', '0', '0', 0, 1, NULL, NULL, ?, ?)",
                (delegation_id, agent_id, str(envelope_usd), now, now),
            )
            payload = {
                "kind": "root",
                "delegation_id": delegation_id,
                "agent_id": agent_id,
                "envelope_usd": envelope_usd,
            }
            if not self._check_event(conn, delegation_id, event_id, payload):
                self._record_event(
                    conn, event_id, delegation_id, "root", payload, envelope_usd, "accepted"
                )
            result = self._get(conn, delegation_id)
            assert result is not None
            return _row_to_state(result)

    def reserve(
        self,
        event_id: str,
        parent_id: str,
        delegation_id: str,
        agent_id: str,
        maximum_usd: Decimal,
    ) -> ReserveOutcome:
        """Atomically bound a new child delegation's authority.

        Returns `"created"` only the one time this call actually creates
        `delegation_id`. Returns `"already_exists"` for any subsequent
        call naming the SAME delegation_id with the SAME parent_id/
        agent_id/maximum_usd -- a safe, idempotent no-op that neither
        re-reserves parent headroom nor changes the child's authority.
        This distinction (not a plain bool) exists so a caller like
        `executor.py` can mint a new capability credential only on
        `"created"`, never on a matching retry -- see repair item 4.

        A reserve call naming an *existing* delegation_id with a
        *different* parent_id, agent_id, or maximum_usd is a conflicting
        reuse of that delegation_id and raises `EventConflict` -- it is
        never silently treated as the original reservation's retry.

        Returns `"rejected"` (no mutation) if `parent_id` or any of its
        ancestors is undergoing revocation (`mark_revocation_started`),
        if the parent lacks headroom, or for any other reason this
        reservation cannot proceed. See the module docstring's
        "Revocation race safety" section.
        """
        validate_identifier("parent_id", parent_id)
        validate_identifier("delegation_id", delegation_id)
        validate_identifier("agent_id", agent_id)
        validate_identifier("event_id", event_id)
        validate_amount(maximum_usd, name="maximum_usd")
        with self._transaction() as conn:
            existing = self._get(conn, delegation_id)
            if existing is not None:
                existing_state = _row_to_state(existing)
                if (
                    existing_state.parent_delegation_id != parent_id
                    or existing_state.agent_id != agent_id
                    or existing_state.authority_usd != maximum_usd
                ):
                    raise EventConflict(
                        f"delegation_id {delegation_id!r} already exists with a different "
                        f"parent_id/agent_id/maximum_usd"
                    )
                return "already_exists"
            if maximum_usd < 0:
                return "rejected"
            parent = self._get(conn, parent_id)
            if parent is None:
                return "rejected"
            parent_state = _row_to_state(parent)
            if not parent_state.active or parent_state.unknown_cost_count:
                return "rejected"
            if self._is_revocation_in_progress_in_lineage(conn, parent_id):
                return "rejected"
            if parent_state.active_reservation_usd < maximum_usd:
                return "rejected"
            now = _now()
            new_child_reserved = str(parent_state.child_reserved_usd + maximum_usd)
            conn.execute(
                "UPDATE delegations SET child_reserved_usd = ?, updated_at = ? "
                "WHERE delegation_id = ?",
                (new_child_reserved, now, parent_id),
            )
            conn.execute(
                "INSERT INTO delegations "
                "(delegation_id, parent_delegation_id, agent_id, authority_usd, "
                "consumed_usd, child_reserved_usd, released_usd, unknown_cost_count, "
                "active, outcome, revocation_started_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, '0', '0', '0', 0, 1, NULL, NULL, ?, ?)",
                (delegation_id, parent_id, agent_id, str(maximum_usd), now, now),
            )
            payload = {
                "kind": "reservation",
                "parent_id": parent_id,
                "delegation_id": delegation_id,
                "agent_id": agent_id,
                "maximum_usd": maximum_usd,
            }
            if not self._check_event(conn, delegation_id, event_id, payload):
                self._record_event(
                    conn, event_id, delegation_id, "reservation", payload, maximum_usd, "accepted"
                )
            return "created"

    # -- mutation -------------------------------------------------------

    def grant(self, event_id: str, delegation_id: str, amount_usd: Decimal) -> bool:
        """Explicitly increase a delegation's authority envelope.

        If `delegation_id` has a parent, the grant is FUNDED from that
        parent's certain headroom: the parent's `child_reserved_usd`
        increases by exactly the same amount as the child's
        `authority_usd`, atomically, and the grant is rejected outright
        if the parent lacks that much headroom -- including when the
        parent has any unknown-cost events recorded, since unknown cost
        is never treated as zero when deciding what's available. A grant
        against a delegation with NO parent (a root) is a pure top-up,
        unconstrained by any parent, since there is nothing to fund it
        from -- this is explicit administrative/policy authority, not
        payment.

        Idempotent on `(delegation_id, event_id)`: replaying the exact
        same grant event must not add the amount twice. A new grant
        decision needs a new event_id -- this store does not invent that
        distinction; the caller does, by choosing whether this is "the
        same grant again" or "a separately authorized additional grant".
        Reusing an event_id already recorded for this delegation with a
        different amount raises `EventConflict`.

        Refuses (returns False) if `delegation_id` or any ancestor is
        undergoing revocation.
        """
        validate_identifier("delegation_id", delegation_id)
        validate_identifier("event_id", event_id)
        validate_amount(amount_usd, name="amount_usd")
        with self._transaction() as conn:
            row = self._get(conn, delegation_id)
            if row is None:
                return False
            state = _row_to_state(row)
            payload = {
                "kind": "authority_grant",
                "delegation_id": delegation_id,
                "amount_usd": amount_usd,
            }
            if self._check_event(conn, delegation_id, event_id, payload):
                return True
            if amount_usd < 0 or not state.active:
                return False
            if self._is_revocation_in_progress_in_lineage(conn, delegation_id):
                return False
            now = _now()
            if state.parent_delegation_id is not None:
                parent = self._get(conn, state.parent_delegation_id)
                if parent is None:
                    return False
                parent_state = _row_to_state(parent)
                if not parent_state.active or parent_state.unknown_cost_count:
                    return False
                if parent_state.active_reservation_usd < amount_usd:
                    return False
                conn.execute(
                    "UPDATE delegations SET child_reserved_usd = ?, updated_at = ? "
                    "WHERE delegation_id = ?",
                    (
                        str(parent_state.child_reserved_usd + amount_usd),
                        now,
                        state.parent_delegation_id,
                    ),
                )
            conn.execute(
                "UPDATE delegations SET authority_usd = ?, updated_at = ? WHERE delegation_id = ?",
                (str(state.authority_usd + amount_usd), now, delegation_id),
            )
            self._record_event(
                conn, event_id, delegation_id, "authority_grant", payload, amount_usd, "accepted"
            )
            return True

    def consume(self, event_id: str, delegation_id: str, amount_usd: Decimal | None) -> bool:
        """Record observed consumption. Idempotent on `(delegation_id,
        event_id)` only.

        Two distinct event_ids against the same delegation are two real
        attempts and both count (accumulation is intentional). Replaying
        one event_id never counts twice, including after a crash between
        commit and the caller observing the response. Reusing an
        event_id already recorded for this delegation with a different
        amount (including known vs. unknown) raises `EventConflict`.

        Refuses (returns False) if `delegation_id` or any ancestor is
        undergoing revocation.
        """
        validate_identifier("delegation_id", delegation_id)
        validate_identifier("event_id", event_id)
        if amount_usd is not None:
            validate_amount(amount_usd, name="amount_usd")
        with self._transaction() as conn:
            row = self._get(conn, delegation_id)
            if row is None:
                return False
            state = _row_to_state(row)
            payload = {
                "kind": "consumption",
                "delegation_id": delegation_id,
                "amount_usd": amount_usd,
            }
            if self._check_event(conn, delegation_id, event_id, payload):
                return True
            if not state.active:
                return False
            if self._is_revocation_in_progress_in_lineage(conn, delegation_id):
                return False
            if amount_usd is None:
                conn.execute(
                    "UPDATE delegations SET unknown_cost_count = unknown_cost_count + 1, "
                    "updated_at = ? WHERE delegation_id = ?",
                    (_now(), delegation_id),
                )
                self._record_event(
                    conn, event_id, delegation_id, "consumption", payload, None, "unknown"
                )
                return True
            available = state.authority_usd - state.child_reserved_usd
            if amount_usd < 0 or state.consumed_usd + amount_usd > available:
                return False
            conn.execute(
                "UPDATE delegations SET consumed_usd = ?, updated_at = ? WHERE delegation_id = ?",
                (str(state.consumed_usd + amount_usd), _now(), delegation_id),
            )
            self._record_event(
                conn, event_id, delegation_id, "consumption", payload, amount_usd, "accepted"
            )
            return True

    def settle(self, event_id: str, delegation_id: str, outcome: str) -> bool:
        """Close a delegation and release its unused reservation.

        Idempotent on `(delegation_id, event_id)`: if a crash happens
        after this commits but before the caller (or its parent) observes
        success, replaying settle with the same event_id is a safe no-op
        -- it will not release the parent's headroom a second time.
        Reusing an event_id already recorded for this delegation with a
        different outcome raises `EventConflict` rather than silently
        reporting success for an outcome that was never actually
        recorded.

        The parent's reservation slot for this child is freed by the full
        original authority amount (including any grants folded into it --
        see `grant`'s parent-funding), but the child's actual known
        consumption is folded into the parent's own `consumed_usd` at the
        same moment -- it does not simply vanish. Without this, a
        reserve-consume-settle cycle would let a chain of settled children
        each really spend money while the parent's tracked state always
        returned to "nothing spent," letting the same dollars be
        re-delegated and re-consumed indefinitely. `consumed_usd` on any
        delegation therefore means "known authority permanently spent,
        directly or through a settled descendant."

        Unknown-cost accounting rule (smallest conservative choice): if
        this delegation recorded any unknown-cost event
        (`unknown_cost_count > 0`), the parent's reservation slot is freed
        by `consumed_usd` only -- never by the full `authority_usd`. The
        remainder (whatever was reserved but neither known-consumed nor
        accounted for) stays counted against the parent's
        `child_reserved_usd` forever, exactly as if it were still an open
        reservation. This deliberately treats "unknown how much was truly
        spent" as "assume the worst, until proven otherwise" rather than
        "assume zero" -- an unresolved unknown cost can never become
        reusable known headroom for the parent, and the parent's own
        `invariant()` keeps reporting PARTIAL certainty (via
        `has_unknown_cost`, which walks the whole subtree including
        settled descendants) rather than silently returning to FULL. There
        is intentionally no mechanism in this store to convert an unknown
        cost back into a known one and reclaim that headroom -- doing so
        safely would require an authoritative source for the true amount,
        which does not exist yet.

        Settling itself is intentionally allowed even while `delegation_id`
        is undergoing revocation -- teardown settles every descendant this
        way, and a settle already in flight for the same reason must not
        be blocked by the very revocation it is part of.
        """
        validate_identifier("delegation_id", delegation_id)
        validate_identifier("event_id", event_id)
        validate_identifier("outcome", outcome)
        with self._transaction() as conn:
            row = self._get(conn, delegation_id)
            if row is None:
                return False
            state = _row_to_state(row)
            payload = {"kind": "settlement", "delegation_id": delegation_id, "outcome": outcome}
            if self._check_event(conn, delegation_id, event_id, payload):
                return True
            if not state.active:
                return False
            released = state.active_reservation_usd
            now = _now()
            conn.execute(
                "UPDATE delegations SET released_usd = ?, active = 0, outcome = ?, updated_at = ? "
                "WHERE delegation_id = ?",
                (str(released), outcome, now, delegation_id),
            )
            if state.parent_delegation_id is not None:
                parent = self._get(conn, state.parent_delegation_id)
                if parent is not None:
                    parent_state = _row_to_state(parent)
                    # See the unknown-cost accounting rule in the docstring
                    # above: a tainted child frees only its known-consumed
                    # amount, never its full authority.
                    parent_reserved_release = (
                        state.consumed_usd if state.unknown_cost_count else state.authority_usd
                    )
                    conn.execute(
                        "UPDATE delegations SET child_reserved_usd = ?, consumed_usd = ?, "
                        "updated_at = ? WHERE delegation_id = ?",
                        (
                            str(parent_state.child_reserved_usd - parent_reserved_release),
                            str(parent_state.consumed_usd + state.consumed_usd),
                            now,
                            state.parent_delegation_id,
                        ),
                    )
            self._record_event(conn, event_id, delegation_id, "settlement", payload, None, outcome)
            return True

    def mark_revocation_started(self, delegation_id: str) -> bool:
        """Durably marks `delegation_id` as undergoing revocation.

        Idempotent: a second call against an already-marked delegation is
        a no-op returning True. Returns False if the delegation does not
        exist. This must be the *first* durable step any tree revocation
        takes -- before computing which descendants currently exist --
        so that `reserve`/`grant`/`consume` (each of which checks this
        flag across the full ancestor chain of the delegation they would
        mutate, inside their own atomic transaction) can never again
        create a new descendant or extend/spend authority anywhere under
        this delegation from the moment this call commits. See the module
        docstring's "Revocation race safety" section for why this closes
        the snapshot-then-settle race, and why a caller that crashes
        between this call and finishing teardown can simply retry the
        whole revoke -- every remaining step is independently idempotent.
        """
        validate_identifier("delegation_id", delegation_id)
        with self._transaction() as conn:
            row = self._get(conn, delegation_id)
            if row is None:
                return False
            if row["revocation_started_at"] is not None:
                return True
            conn.execute(
                "UPDATE delegations SET revocation_started_at = ?, updated_at = ? "
                "WHERE delegation_id = ?",
                (_now(), _now(), delegation_id),
            )
            return True

    # -- reads -------------------------------------------------------

    def get(self, delegation_id: str) -> DelegationState | None:
        with self._transaction() as conn:
            row = self._get(conn, delegation_id)
            return None if row is None else _row_to_state(row)

    def children(self, delegation_id: str) -> list[DelegationState]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM delegations WHERE parent_delegation_id = ? ORDER BY created_at",
                (delegation_id,),
            ).fetchall()
            return [_row_to_state(row) for row in rows]

    def lineage(self, delegation_id: str) -> list[DelegationState]:
        """Root-to-leaf ancestor chain, reconstructed purely from durable state."""
        with self._transaction() as conn:
            chain: list[DelegationState] = []
            current: str | None = delegation_id
            while current is not None:
                row = self._get(conn, current)
                if row is None:
                    break
                state = _row_to_state(row)
                chain.append(state)
                current = state.parent_delegation_id
            return list(reversed(chain))

    def has_unknown_cost(self, delegation_id: str) -> bool:
        """True if this delegation or any descendant recorded an unknown-cost event."""
        with self._transaction() as conn:
            stack = [delegation_id]
            while stack:
                current = stack.pop()
                row = self._get(conn, current)
                if row is not None and row["unknown_cost_count"]:
                    return True
                children = conn.execute(
                    "SELECT delegation_id FROM delegations WHERE parent_delegation_id = ?",
                    (current,),
                ).fetchall()
                stack.extend(child["delegation_id"] for child in children)
            return False

    def invariant(self, delegation_id: str) -> InvariantResult:
        """Check the conservation invariant for one delegation.

            known_consumed + active_child_reservations <= delegated_authority

        plus non-negativity of every tracked quantity. When any unknown
        cost event exists anywhere in this delegation's subtree, the
        result is explicitly labeled PARTIAL certainty rather than
        silently reported as a clean SATISFIED -- an unknown cost means
        exact available authority cannot be claimed, so equality/adequacy
        beyond the known figures is not asserted either way.
        """
        state = self.get(delegation_id)
        if state is None:
            return InvariantResult(
                delegation_id, False, "FULL", "MISSING", "delegation does not exist"
            )
        non_negative = (
            state.consumed_usd >= 0
            and state.released_usd >= 0
            and state.child_reserved_usd >= 0
            and state.active_reservation_usd >= 0
        )
        conserved = state.consumed_usd + state.child_reserved_usd <= state.authority_usd
        satisfied = non_negative and conserved
        has_unknown = self.has_unknown_cost(delegation_id)
        certainty: Literal["FULL", "PARTIAL"] = "PARTIAL" if has_unknown else "FULL"
        if not satisfied:
            label = "VIOLATED"
        elif certainty == "FULL":
            label = "SATISFIED"
        else:
            label = "NOT VIOLATED ON KNOWN VALUES (PARTIAL)"
        detail = (
            f"consumed={state.consumed_usd} child_reserved={state.child_reserved_usd} "
            f"authority={state.authority_usd} released={state.released_usd} "
            f"unknown_cost_count={state.unknown_cost_count}"
        )
        return InvariantResult(delegation_id, satisfied, certainty, label, detail)
