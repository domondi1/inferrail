"""Inferrail Economic Authority — durable economic-authority core.

Given a caller-declared spending ceiling for a unit of work (a
"delegation"), this module tracks how much of that ceiling has been
reserved for sub-work, actually consumed, and released — atomically,
durably, and honestly about what is known versus unknown.

This module is transport-independent: it knows nothing about A2A, HTTP,
or payment. A transport adapter (added separately) is responsible for
authenticating a caller, extracting a `delegation_id` from whatever
protocol it speaks, and calling this store. This module is the sole
authority for economic state.

Core distinction this module exists to enforce:

    delegation_id  -- durable economic identity. Created once by whoever
                      opens the delegation. Stable across retries,
                      transport reconnects, and process restarts.

    event_id       -- idempotency key for one economic action attempt
                      (reserve/grant/consume/settle). A duplicate
                      delivery of the same attempt reuses the same
                      event_id and must not mutate state twice. A
                      legitimate new attempt (a real retry with new
                      information, or a second real charge) uses a new
                      event_id and may have a real economic effect.

Concurrency and crash safety come from SQLite: every mutating operation
runs inside a single `BEGIN IMMEDIATE` transaction, which takes SQLite's
write lock before reading state, so two callers racing to reserve the
same headroom serialize correctly instead of both observing the
pre-reservation balance. Commits are durable, so a process killed
immediately after a commit still has that effect on restart, and a
process killed before commit has none of it -- there is no partial state
to reconcile.

Money is stored as canonical Decimal strings and never as float. Unknown
cost is tracked explicitly (a count, never folded into a fabricated
total) -- the same "unknown stays unknown" discipline already shipped for
`inferrail.receipts.InferenceReceipt` and `hosted/work_economics`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

Kind = Literal["root", "reservation", "authority_grant", "consumption", "settlement"]


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
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


SCHEMA = """
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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS economic_events (
    event_id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount_usd TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class EconomicAuthorityStore:
    """SQLite-backed authoritative economic state for one deployment.

    One store instance may be opened by many processes against the same
    `db_path`; each mutating call opens its own short-lived connection so
    no connection is held open across a crash-injection boundary.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)  # executescript manages its own transaction
        finally:
            conn.close()

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
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _get(self, conn: sqlite3.Connection, delegation_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM delegations WHERE delegation_id = ?", (delegation_id,)
        ).fetchone()

    def _event_exists(self, conn: sqlite3.Connection, event_id: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM economic_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            is not None
        )

    def _record_event(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        delegation_id: str,
        kind: Kind,
        amount_usd: Decimal | None,
        status: str,
    ) -> None:
        amount = None if amount_usd is None else str(amount_usd)
        conn.execute(
            "INSERT INTO economic_events "
            "(event_id, delegation_id, kind, amount_usd, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, delegation_id, kind, amount, status, _now()),
        )

    # -- creation -----------------------------------------------------

    def create_root(
        self, event_id: str, delegation_id: str, agent_id: str, envelope_usd: Decimal
    ) -> DelegationState:
        """Idempotent bootstrap of a root (parentless) delegation.

        A duplicate call with the same `delegation_id` -- whatever
        `event_id` it arrives with -- returns the existing row unchanged.
        Root authority can only be created here, once, per delegation_id;
        it is never re-minted by replay.
        """
        with self._transaction() as conn:
            existing = self._get(conn, delegation_id)
            if existing is not None:
                return _row_to_state(existing)
            if envelope_usd < 0:
                raise ValueError("envelope must be non-negative")
            now = _now()
            conn.execute(
                "INSERT INTO delegations "
                "(delegation_id, parent_delegation_id, agent_id, authority_usd, "
                "consumed_usd, child_reserved_usd, released_usd, unknown_cost_count, "
                "active, outcome, created_at, updated_at) "
                "VALUES (?, NULL, ?, ?, '0', '0', '0', 0, 1, NULL, ?, ?)",
                (delegation_id, agent_id, str(envelope_usd), now, now),
            )
            if not self._event_exists(conn, event_id):
                self._record_event(
                    conn, event_id, delegation_id, "root", envelope_usd, "accepted"
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
    ) -> bool:
        """Atomically bound a new child delegation's authority.

        Idempotency is keyed on `delegation_id`, not `event_id`: once a
        delegation exists, ANY further reserve call naming it -- same
        event_id or a caller-assigned different one -- is a no-op that
        neither re-reserves parent headroom nor changes the child's
        authority. This is what makes duplicate message delivery safe
        even if the transport layer above this store does not itself
        deduplicate.
        """
        with self._transaction() as conn:
            if self._get(conn, delegation_id) is not None:
                return True
            if maximum_usd < 0:
                return False
            parent = self._get(conn, parent_id)
            if parent is None:
                return False
            parent_state = _row_to_state(parent)
            if not parent_state.active or parent_state.unknown_cost_count:
                return False
            if parent_state.active_reservation_usd < maximum_usd:
                return False
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
                "active, outcome, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, '0', '0', '0', 0, 1, NULL, ?, ?)",
                (delegation_id, parent_id, agent_id, str(maximum_usd), now, now),
            )
            if not self._event_exists(conn, event_id):
                self._record_event(
                    conn, event_id, delegation_id, "reservation", maximum_usd, "accepted"
                )
            return True

    # -- mutation -------------------------------------------------------

    def grant(self, event_id: str, delegation_id: str, amount_usd: Decimal) -> bool:
        """Explicitly increase a delegation's authority envelope.

        Idempotent on `event_id`: replaying the exact same grant event
        must not add the amount twice. A new grant decision needs a new
        event_id -- this store does not invent that distinction; the
        caller does, by choosing whether this is "the same grant again"
        or "a separately authorized additional grant."
        """
        with self._transaction() as conn:
            if self._event_exists(conn, event_id):
                return True
            row = self._get(conn, delegation_id)
            if row is None:
                return False
            state = _row_to_state(row)
            if amount_usd < 0 or not state.active:
                return False
            conn.execute(
                "UPDATE delegations SET authority_usd = ?, updated_at = ? WHERE delegation_id = ?",
                (str(state.authority_usd + amount_usd), _now(), delegation_id),
            )
            self._record_event(
                conn, event_id, delegation_id, "authority_grant", amount_usd, "accepted"
            )
            return True

    def consume(self, event_id: str, delegation_id: str, amount_usd: Decimal | None) -> bool:
        """Record observed consumption. Idempotent on `event_id` only.

        Two distinct event_ids against the same delegation are two real
        attempts and both count (accumulation is intentional). Replaying
        one event_id never counts twice, including after a crash between
        commit and the caller observing the response.
        """
        with self._transaction() as conn:
            if self._event_exists(conn, event_id):
                return True
            row = self._get(conn, delegation_id)
            if row is None:
                return False
            state = _row_to_state(row)
            if not state.active:
                return False
            if amount_usd is None:
                conn.execute(
                    "UPDATE delegations SET unknown_cost_count = unknown_cost_count + 1, "
                    "updated_at = ? WHERE delegation_id = ?",
                    (_now(), delegation_id),
                )
                self._record_event(
                    conn, event_id, delegation_id, "consumption", None, "unknown"
                )
                return True
            available = state.authority_usd - state.child_reserved_usd
            if amount_usd < 0 or state.consumed_usd + amount_usd > available:
                return False
            conn.execute(
                "UPDATE delegations SET consumed_usd = ?, updated_at = ? WHERE delegation_id = ?",
                (str(state.consumed_usd + amount_usd), _now(), delegation_id),
            )
            self._record_event(conn, event_id, delegation_id, "consumption", amount_usd, "accepted")
            return True

    def settle(self, event_id: str, delegation_id: str, outcome: str) -> bool:
        """Close a delegation and release its unused reservation.

        Idempotent on `event_id`: if a crash happens after this commits
        but before the caller (or its parent) observes success, replaying
        settle with the same event_id is a safe no-op -- it will not
        release the parent's headroom a second time.

        The parent's reservation slot for this child is freed by the full
        original authority amount, but the child's actual known
        consumption is folded into the parent's own `consumed_usd` at the
        same moment -- it does not simply vanish. Without this, a
        reserve-consume-settle cycle would let a chain of settled children
        each really spend money while the parent's tracked state always
        returned to "nothing spent," letting the same dollars be
        re-delegated and re-consumed indefinitely. `consumed_usd` on any
        delegation therefore means "known authority permanently spent,
        directly or through a settled descendant."
        """
        with self._transaction() as conn:
            if self._event_exists(conn, event_id):
                return True
            row = self._get(conn, delegation_id)
            if row is None:
                return False
            state = _row_to_state(row)
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
                    conn.execute(
                        "UPDATE delegations SET child_reserved_usd = ?, consumed_usd = ?, "
                        "updated_at = ? WHERE delegation_id = ?",
                        (
                            str(parent_state.child_reserved_usd - state.authority_usd),
                            str(parent_state.consumed_usd + state.consumed_usd),
                            now,
                            state.parent_delegation_id,
                        ),
                    )
            self._record_event(conn, event_id, delegation_id, "settlement", None, outcome)
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
