"""Tests for hosted/a2a_economic_authority/core.py's durable economic-authority
core. No network, no secrets, no A2A/x402 dependency -- this module is
transport-independent and so are these tests.
"""

from __future__ import annotations

import inspect
import sqlite3
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTED_DIR = REPO_ROOT / "hosted" / "a2a_economic_authority"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

import pytest  # noqa: E402
from core import EconomicAuthorityStore, EventConflict  # noqa: E402

CRASH_HELPER = Path(__file__).resolve().parent / "_a2a_economic_authority_crash_helper.py"


@pytest.fixture
def store(tmp_path) -> EconomicAuthorityStore:
    return EconomicAuthorityStore(tmp_path / "authority.sqlite3")


def _open_root(store: EconomicAuthorityStore, authority_usd: str = "1.00") -> None:
    store.create_root("evt:root", "root", "buyer", Decimal(authority_usd))


# -- basic lifecycle --------------------------------------------------


def test_reserve_consume_settle_happy_path(store: EconomicAuthorityStore):
    _open_root(store)
    assert store.reserve("evt:reserve", "root", "child-1", "worker", Decimal("0.30")) == "created"
    assert store.consume("evt:consume", "child-1", Decimal("0.10")) is True
    assert store.settle("evt:settle", "child-1", "SUCCESS") is True

    child = store.get("child-1")
    assert child is not None
    assert child.consumed_usd == Decimal("0.10")
    assert child.active is False
    assert child.outcome == "SUCCESS"


# -- overrun rejection --------------------------------------------------


def test_reserve_rejects_when_exceeding_parent_headroom(store: EconomicAuthorityStore):
    _open_root(store, "1.00")
    assert store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.60")) == "created"
    # Only $0.40 headroom remains; a $0.60 reservation must be denied, not
    # partially granted or silently capped.
    assert store.reserve("evt:r2", "root", "child-2", "worker", Decimal("0.60")) == "rejected"
    assert store.get("child-2") is None


def test_consume_rejects_when_exceeding_available_authority(store: EconomicAuthorityStore):
    _open_root(store, "0.50")
    assert store.consume("evt:c1", "root", Decimal("0.30")) is True
    assert store.consume("evt:c2", "root", Decimal("0.30")) is False
    root = store.get("root")
    assert root is not None
    assert root.consumed_usd == Decimal("0.30")


# -- concurrency: competing reservations cannot exceed available authority ---


def test_concurrent_reservations_cannot_exceed_available_authority(tmp_path):
    """Two real OS threads race to reserve $0.70 each from a $1.00 root.

    Together they would overrun (`$1.40 > $1.00`); the store's atomic
    `BEGIN IMMEDIATE` transaction must serialize the race so exactly one
    reservation succeeds, not both and not neither.
    """
    db_path = tmp_path / "authority.sqlite3"
    store = EconomicAuthorityStore(db_path)
    _open_root(store, "1.00")

    def try_reserve(child_id: str) -> str:
        # Each thread uses its own EconomicAuthorityStore instance (and
        # therefore its own SQLite connection) against the same db file,
        # matching how independent concurrent callers would behave.
        return EconomicAuthorityStore(db_path).reserve(
            f"evt:{child_id}", "root", child_id, "worker", Decimal("0.70")
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(try_reserve, "child-a")
        future_b = pool.submit(try_reserve, "child-b")
        results = {future_a.result(), future_b.result()}

    assert results == {"created", "rejected"}, (
        "exactly one of two competing $0.70 reservations must succeed"
    )
    root = store.get("root")
    assert root is not None
    # Whichever one succeeded, exactly $0.70 (not $0.00, not $1.40) is reserved.
    assert root.child_reserved_usd == Decimal("0.70")
    assert root.active_reservation_usd == Decimal("0.30")


# -- idempotency: duplicate event_id vs. new event_id --------------------


def test_duplicate_event_id_does_not_double_count(store: EconomicAuthorityStore):
    _open_root(store, "1.00")
    store.reserve("evt:reserve", "root", "child-1", "worker", Decimal("0.50"))
    assert store.consume("evt:dup", "child-1", Decimal("0.10")) is True
    # Replaying the exact same event_id (a duplicate delivery) must not
    # add the amount a second time.
    assert store.consume("evt:dup", "child-1", Decimal("0.10")) is True
    child = store.get("child-1")
    assert child is not None
    assert child.consumed_usd == Decimal("0.10")


def test_new_event_id_is_a_real_second_charge(store: EconomicAuthorityStore):
    _open_root(store, "1.00")
    store.reserve("evt:reserve", "root", "child-1", "worker", Decimal("0.50"))
    store.consume("evt:c1", "child-1", Decimal("0.10"))
    store.consume("evt:c2", "child-1", Decimal("0.10"))
    child = store.get("child-1")
    assert child is not None
    assert child.consumed_usd == Decimal("0.20")


def test_duplicate_delegation_id_reserve_is_a_no_op(store: EconomicAuthorityStore):
    _open_root(store, "1.00")
    assert store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.50")) == "created"
    # A second reserve call naming the same delegation_id -- even with a
    # different event_id, as a duplicate transport delivery would produce
    # -- must not re-reserve parent headroom.
    assert (
        store.reserve("evt:r2-different", "root", "child-1", "worker", Decimal("0.50"))
        == "already_exists"
    )
    root = store.get("root")
    assert root is not None
    assert root.child_reserved_usd == Decimal("0.50")


def test_grant_is_idempotent_on_event_id(store: EconomicAuthorityStore):
    _open_root(store, "1.00")
    assert store.grant("evt:grant", "root", Decimal("0.20")) is True
    assert store.grant("evt:grant", "root", Decimal("0.20")) is True
    root = store.get("root")
    assert root is not None
    assert root.authority_usd == Decimal("1.20")


# -- unknown cost stays explicitly uncertain --------------------------


def test_unknown_cost_is_never_fabricated_and_marks_partial_certainty(
    store: EconomicAuthorityStore,
):
    _open_root(store, "1.00")
    assert store.consume("evt:unknown", "root", None) is True
    root = store.get("root")
    assert root is not None
    assert root.unknown_cost_count == 1
    assert root.consumed_usd == Decimal("0")  # never guessed

    result = store.invariant("root")
    assert result.certainty == "PARTIAL"
    assert result.label == "NOT VIOLATED ON KNOWN VALUES (PARTIAL)"


def test_invariant_is_full_certainty_with_no_unknown_cost(store: EconomicAuthorityStore):
    _open_root(store, "1.00")
    store.consume("evt:c1", "root", Decimal("0.10"))
    result = store.invariant("root")
    assert result.certainty == "FULL"
    assert result.label == "SATISFIED"


# -- finding 2: settling a child with unknown cost must not free reusable --
# -- headroom for the parent -----------------------------------------------


def test_settling_a_child_with_unknown_cost_never_frees_reusable_parent_headroom(
    store: EconomicAuthorityStore,
):
    """Regression test for finding 2 (independent review round 3): root
    reserves part of its authority for a child, the child records an
    UNKNOWN-cost event (the true amount was never resolved), and the
    child settles. Before the fix, settle() released the child's full
    authority back to the parent's headroom regardless of unknown cost,
    treating the unresolved amount as though it were certainly zero --
    root could then reserve its full original authority again even though
    part of it might have actually been spent by the settled child.

    Correct (smallest conservative) behavior: a settled child that ever
    recorded unknown cost releases only its KNOWN consumption back to the
    parent; the remainder stays counted against the parent's
    `child_reserved_usd` forever -- unavailable, not reusable, not
    silently discarded either (conservation still holds) -- and the
    parent's own invariant keeps reporting PARTIAL certainty because the
    settled child (still present in its subtree) still carries the
    unknown-cost event.
    """
    _open_root(store, "1.00")
    store.reserve("evt:reserve", "root", "child-unknown", "worker", Decimal("0.40"))
    assert store.consume("evt:unknown-spend", "child-unknown", None) is True
    assert store.settle("evt:settle", "child-unknown", "PARTIAL") is True

    root = store.get("root")
    assert root is not None
    # None of the $0.40 reserved for this child is released back to root
    # -- the unresolved unknown cost could be anywhere up to that amount.
    assert root.child_reserved_usd == Decimal("0.40")
    assert root.consumed_usd == Decimal("0")
    assert root.active_reservation_usd == Decimal("0.60")

    # Root must never be able to reserve the full original $1.00 again --
    # only the genuinely untouched $0.60 remains available.
    assert store.reserve("evt:reserve-full", "root", "child-2", "worker", Decimal("1.00")) == (
        "rejected"
    )
    assert store.reserve("evt:reserve-remaining", "root", "child-3", "worker", Decimal("0.60")) == (
        "created"
    )
    assert (
        store.reserve("evt:reserve-over", "root", "child-4", "worker", Decimal("0.01"))
        == "rejected"
    )

    result = store.invariant("root")
    assert result.certainty == "PARTIAL", (
        "an unresolved unknown cost from a settled child must keep the parent's "
        "invariant at PARTIAL certainty forever, never silently return to FULL"
    )
    assert result.satisfied_on_known_values is True


def test_settling_a_child_with_partial_known_and_unknown_cost_releases_only_the_known_part(
    store: EconomicAuthorityStore,
):
    """A child that recorded BOTH a known consumption and an unknown-cost
    event still only releases the known amount to the parent -- the
    unknown-tainted remainder is never assumed to be free just because
    part of the child's activity happened to be resolved."""
    _open_root(store, "1.00")
    store.reserve("evt:reserve", "root", "child-mixed", "worker", Decimal("0.50"))
    assert store.consume("evt:known-spend", "child-mixed", Decimal("0.10")) is True
    assert store.consume("evt:unknown-spend", "child-mixed", None) is True
    assert store.settle("evt:settle", "child-mixed", "PARTIAL") is True

    root = store.get("root")
    assert root is not None
    # Only the known $0.10 is folded into root's consumed_usd and released
    # from child_reserved_usd; the remaining $0.40 stays locked.
    assert root.consumed_usd == Decimal("0.10")
    assert root.child_reserved_usd == Decimal("0.40")
    assert root.active_reservation_usd == Decimal("0.50")


def test_settling_a_child_with_no_unknown_cost_still_releases_its_full_authority(
    store: EconomicAuthorityStore,
):
    """The conservative unknown-cost rule must not regress the ordinary,
    fully-known case: a child with zero unknown-cost events still frees
    its complete unused reservation back to the parent on settlement,
    exactly as before."""
    _open_root(store, "1.00")
    store.reserve("evt:reserve", "root", "child-known-only", "worker", Decimal("0.40"))
    store.consume("evt:known-spend", "child-known-only", Decimal("0.15"))
    store.settle("evt:settle", "child-known-only", "SUCCESS")

    root = store.get("root")
    assert root is not None
    assert root.child_reserved_usd == Decimal("0")
    assert root.consumed_usd == Decimal("0.15")
    assert root.active_reservation_usd == Decimal("0.85")
    assert store.invariant("root").certainty == "FULL"


# -- the previously-fixed parent/child settlement defect: regression test ---


def test_settlement_folds_child_consumption_into_parent_conservation(store: EconomicAuthorityStore):
    """Regression test for a real defect found and fixed in the private
    prototype: settling a child used to free the parent's reservation
    without recording the child's actual consumption against the parent,
    which would let the same dollars be re-delegated and re-consumed
    indefinitely across repeated reserve/consume/settle cycles.

    Correct behavior: after settlement, the parent's `child_reserved_usd`
    drops by the child's full authority, and the parent's `consumed_usd`
    increases by exactly the child's actual consumption -- the spent
    portion does not vanish, and the unspent portion is available again.
    """
    _open_root(store, "1.00")
    store.reserve("evt:reserve", "root", "child-1", "worker", Decimal("0.50"))
    store.consume("evt:consume", "child-1", Decimal("0.30"))
    store.settle("evt:settle", "child-1", "SUCCESS")

    root = store.get("root")
    assert root is not None
    assert root.child_reserved_usd == Decimal("0")
    assert root.consumed_usd == Decimal("0.30")
    assert root.active_reservation_usd == Decimal("0.70")

    result = store.invariant("root")
    assert result.satisfied_on_known_values is True
    assert result.label == "SATISFIED"

    # A second reserve/consume/settle cycle against the freed headroom
    # must not be able to re-spend the $0.30 already recorded as consumed.
    store.reserve("evt:reserve2", "root", "child-2", "worker", Decimal("0.70"))
    assert store.consume("evt:overspend", "child-2", Decimal("0.71")) is False


# -- lineage / children read views --------------------------------------


def test_lineage_and_children_reflect_the_delegation_tree(store: EconomicAuthorityStore):
    _open_root(store, "1.00")
    store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.50"))
    store.reserve("evt:r2", "child-1", "grandchild-1", "sub-worker", Decimal("0.20"))

    lineage = store.lineage("grandchild-1")
    assert [state.delegation_id for state in lineage] == ["root", "child-1", "grandchild-1"]

    children = store.children("root")
    assert [state.delegation_id for state in children] == ["child-1"]


# -- crash / restart durability ------------------------------------------


def test_state_survives_real_process_termination_and_restart(tmp_path):
    db_path = tmp_path / "authority.sqlite3"
    store = EconomicAuthorityStore(db_path)
    _open_root(store, "1.00")

    for boundary in ("after_reserve", "after_consume", "after_settle"):
        result = subprocess.run(
            [sys.executable, str(CRASH_HELPER), str(db_path), boundary],
            capture_output=True,
            text=True,
            timeout=15,
        )
        # os._exit(1) means the process never returns 0 -- the crash is
        # real, not a clean shutdown that happens to also mutate state.
        assert result.returncode != 0, (
            f"helper for {boundary!r} exited cleanly (code {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    # A fresh store instance, opened after every crash, sees the fully
    # committed effect of all three mutations -- nothing lost, nothing
    # double-applied.
    fresh_store = EconomicAuthorityStore(db_path)
    child = fresh_store.get("child-1")
    assert child is not None
    assert child.consumed_usd == Decimal("0.10")
    assert child.active is False
    assert child.outcome == "SUCCESS"

    root = fresh_store.get("root")
    assert root is not None
    assert root.child_reserved_usd == Decimal("0")
    assert root.consumed_usd == Decimal("0.10")


# -- idempotency is scoped per delegation, not global (repair item 5) ------


def test_same_event_id_on_two_independent_delegations_does_not_collide(
    store: EconomicAuthorityStore,
):
    """Two unrelated delegations picking the same caller-chosen event_id
    must not affect each other -- the idempotency key is (delegation_id,
    event_id), never event_id alone."""
    _open_root(store, "1.00")
    store.reserve("evt:r1", "root", "child-a", "worker", Decimal("0.30"))
    store.reserve("evt:r2", "root", "child-b", "worker", Decimal("0.30"))

    assert store.grant("evt:shared", "child-a", Decimal("0.10")) is True
    assert store.grant("evt:shared", "child-b", Decimal("0.20")) is True

    child_a = store.get("child-a")
    child_b = store.get("child-b")
    assert child_a is not None and child_a.authority_usd == Decimal("0.40")
    assert child_b is not None and child_b.authority_usd == Decimal("0.50")


def test_same_event_id_on_two_independent_delegations_does_not_collide_for_consume(
    store: EconomicAuthorityStore,
):
    _open_root(store, "1.00")
    store.reserve("evt:r1", "root", "child-a", "worker", Decimal("0.30"))
    store.reserve("evt:r2", "root", "child-b", "worker", Decimal("0.30"))

    assert store.consume("evt:shared", "child-a", Decimal("0.05")) is True
    assert store.consume("evt:shared", "child-b", Decimal("0.07")) is True

    assert store.get("child-a").consumed_usd == Decimal("0.05")  # type: ignore[union-attr]
    assert store.get("child-b").consumed_usd == Decimal("0.07")  # type: ignore[union-attr]


def test_grant_reusing_event_id_with_different_amount_raises_conflict(
    store: EconomicAuthorityStore,
):
    _open_root(store, "1.00")
    assert store.grant("evt:g1", "root", Decimal("0.10")) is True
    with pytest.raises(EventConflict):
        store.grant("evt:g1", "root", Decimal("0.20"))
    # The original grant's effect is untouched by the rejected conflict.
    assert store.get("root").authority_usd == Decimal("1.10")  # type: ignore[union-attr]


def test_consume_reusing_event_id_with_different_amount_raises_conflict(
    store: EconomicAuthorityStore,
):
    _open_root(store, "1.00")
    assert store.consume("evt:c1", "root", Decimal("0.10")) is True
    with pytest.raises(EventConflict):
        store.consume("evt:c1", "root", Decimal("0.20"))
    assert store.get("root").consumed_usd == Decimal("0.10")  # type: ignore[union-attr]


def test_consume_reusing_event_id_known_vs_unknown_raises_conflict(store: EconomicAuthorityStore):
    _open_root(store, "1.00")
    assert store.consume("evt:c1", "root", Decimal("0.10")) is True
    with pytest.raises(EventConflict):
        store.consume("evt:c1", "root", None)


def test_settle_reusing_event_id_with_different_outcome_raises_conflict(
    store: EconomicAuthorityStore,
):
    _open_root(store, "1.00")
    store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.30"))
    assert store.settle("evt:s1", "child-1", "SUCCESS") is True
    with pytest.raises(EventConflict):
        store.settle("evt:s1", "child-1", "FAIL")
    assert store.get("child-1").outcome == "SUCCESS"  # type: ignore[union-attr]


def test_reserve_reusing_delegation_id_with_different_parent_raises_conflict(
    store: EconomicAuthorityStore,
):
    _open_root(store, "1.00")
    store.reserve("evt:r1", "root", "sub-1", "worker", Decimal("0.10"))
    store.reserve("evt:r2", "root", "child-1", "worker", Decimal("0.30"))
    with pytest.raises(EventConflict):
        # Same delegation_id "child-1", but a different parent_id than the
        # one it was actually created under.
        store.reserve("evt:r3-different", "sub-1", "child-1", "worker", Decimal("0.30"))


def test_reserve_reusing_delegation_id_with_different_amount_raises_conflict(
    store: EconomicAuthorityStore,
):
    _open_root(store, "1.00")
    store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.30"))
    with pytest.raises(EventConflict):
        store.reserve("evt:r2-different", "root", "child-1", "worker", Decimal("0.40"))


def test_reserve_reusing_delegation_id_with_different_agent_raises_conflict(
    store: EconomicAuthorityStore,
):
    _open_root(store, "1.00")
    store.reserve("evt:r1", "root", "child-1", "worker-a", Decimal("0.30"))
    with pytest.raises(EventConflict):
        store.reserve("evt:r2-different", "root", "child-1", "worker-b", Decimal("0.30"))


def test_create_root_reusing_delegation_id_with_different_envelope_raises_conflict(
    store: EconomicAuthorityStore,
):
    store.create_root("evt:root1", "root", "buyer", Decimal("1.00"))
    with pytest.raises(EventConflict):
        store.create_root("evt:root2-different", "root", "buyer", Decimal("2.00"))


def test_matching_retry_of_reserve_is_still_a_safe_no_op(store: EconomicAuthorityStore):
    """The idempotency fix must not regress the original guarantee: a
    retry with the SAME canonical payload (parent/agent/amount) remains a
    safe, non-conflicting no-op."""
    _open_root(store, "1.00")
    assert store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.30")) == "created"
    assert (
        store.reserve("evt:r2-different", "root", "child-1", "worker", Decimal("0.30"))
        == "already_exists"
    )
    root = store.get("root")
    assert root is not None
    assert root.child_reserved_usd == Decimal("0.30")


# -- revocation epoch: reserve refuses once revocation has started (repair item 4) --


def test_mark_revocation_started_is_idempotent_and_reports_missing_delegation(
    store: EconomicAuthorityStore,
):
    assert store.mark_revocation_started("does-not-exist") is False
    _open_root(store, "1.00")
    assert store.mark_revocation_started("root") is True
    assert store.mark_revocation_started("root") is True  # second call, same effect
    root = store.get("root")
    assert root is not None
    assert root.revocation_started_at is not None


def test_reserve_refuses_once_direct_parent_revocation_has_started(store: EconomicAuthorityStore):
    _open_root(store, "1.00")
    store.mark_revocation_started("root")
    assert store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.10")) == "rejected"
    assert store.get("child-1") is None


def test_reserve_refuses_once_any_ancestor_revocation_has_started(store: EconomicAuthorityStore):
    """The flag is checked across the WHOLE ancestor chain, not just the
    direct parent -- this is what lets marking the root block a brand-new
    reservation several levels deep."""
    _open_root(store, "1.00")
    store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.50"))
    store.reserve("evt:r2", "child-1", "grandchild-1", "worker", Decimal("0.20"))
    store.mark_revocation_started("root")
    assert (
        store.reserve("evt:r3", "grandchild-1", "great-grandchild-1", "worker", Decimal("0.01"))
        == "rejected"
    )
    assert store.get("great-grandchild-1") is None


def test_reserve_before_revocation_mark_still_succeeds(store: EconomicAuthorityStore):
    """The flag only blocks reservations that observe it -- one that
    genuinely committed first is unaffected (and will be caught by a
    subsequent subtree scan/settle, as `executor.py` performs)."""
    _open_root(store, "1.00")
    assert store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.10")) == "created"
    store.mark_revocation_started("root")
    child = store.get("child-1")
    assert child is not None
    assert child.active is True  # marking alone does not settle anything


def test_concurrent_reserve_and_revocation_mark_never_lets_a_reservation_escape_unmarked(tmp_path):
    """Whitebox concurrency proof for the race this repair closes: many
    threads race real `reserve` calls against a `mark_revocation_started`
    call for the same parent. Every reservation that the store reports as
    successful must have committed strictly before the mark (and is
    therefore visible to a subtree scan taken after the mark); every
    reservation attempted once the mark is visible must be refused. There
    is no possible outcome where a reservation is both reported successful
    AND unreachable by a post-mark scan.
    """
    db_path = tmp_path / "authority.sqlite3"
    store = EconomicAuthorityStore(db_path)
    store.create_root("evt:root", "root", "buyer", Decimal("100.00"))

    def try_reserve(i: int) -> str:
        return EconomicAuthorityStore(db_path).reserve(
            f"evt:race-{i}", "root", f"child-race-{i}", "worker", Decimal("0.01")
        )

    def do_mark() -> bool:
        return EconomicAuthorityStore(db_path).mark_revocation_started("root")

    with ThreadPoolExecutor(max_workers=17) as pool:
        reserve_futures = [pool.submit(try_reserve, i) for i in range(16)]
        mark_future = pool.submit(do_mark)
        results = [f.result() for f in reserve_futures]
        assert mark_future.result() is True

    # The mark itself is now durably visible. A subtree scan performed
    # AFTER this point (as executor.py's revoke does) must see every
    # delegation that reserve() reported as created -- prove that here by
    # checking each one directly.
    root_after = store.get("root")
    assert root_after is not None
    assert root_after.revocation_started_at is not None

    successful_children = [f"child-race-{i}" for i, ok in enumerate(results) if ok == "created"]
    for child_id in successful_children:
        child = store.get(child_id)
        assert child is not None, (
            f"{child_id} was reported reserved but does not exist -- "
            "a committed reservation must never be invisible"
        )

    # No further reservation can land against root now that it is marked,
    # regardless of how many succeeded before the mark.
    assert (
        store.reserve("evt:race-after", "root", "child-after-mark", "worker", Decimal("0.01"))
        == "rejected"
    )
    assert store.get("child-after-mark") is None


def test_grant_and_consume_refuse_once_ancestor_revocation_has_started(
    store: EconomicAuthorityStore,
):
    _open_root(store, "1.00")
    store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.30"))
    store.mark_revocation_started("root")
    assert store.grant("evt:g1", "child-1", Decimal("0.01")) is False
    assert store.consume("evt:c1", "child-1", Decimal("0.01")) is False
    child = store.get("child-1")
    assert child is not None
    assert child.authority_usd == Decimal("0.30")  # untouched
    assert child.consumed_usd == Decimal("0")  # untouched


# -- migration: an authentic old-schema database upgrades safely (repair item 1) --


def _write_legacy_phase_a_database(db_path: Path) -> None:
    """Builds an authentic pre-repair (Phase A) schema database by hand --
    global `event_id TEXT PRIMARY KEY`, no `revocation_started_at`, no
    `schema_meta` -- with real delegation and event data, exactly as a
    deployed Phase A database would look."""
    legacy_schema = """
    CREATE TABLE delegations (
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
    CREATE TABLE economic_events (
        event_id TEXT PRIMARY KEY,
        delegation_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        amount_usd TEXT,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(legacy_schema)
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO delegations VALUES "
            "('root', NULL, 'buyer', '1.00', '0.10', '0.30', '0', 0, 1, NULL, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO delegations VALUES "
            "('child-1', 'root', 'worker', '0.30', '0.10', '0', '0', 0, 1, NULL, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO economic_events VALUES "
            "('evt:root', 'root', 'root', '1.00', 'accepted', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO economic_events VALUES "
            "('evt:r1', 'child-1', 'reservation', '0.30', 'accepted', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO economic_events VALUES "
            "('evt:c1', 'child-1', 'consumption', '0.10', 'accepted', ?)",
            (now,),
        )
        conn.commit()
    finally:
        conn.close()


def test_migration_preserves_data_and_upgrades_schema(tmp_path):
    db_path = tmp_path / "authority.sqlite3"
    _write_legacy_phase_a_database(db_path)

    store = EconomicAuthorityStore(db_path)

    root = store.get("root")
    child = store.get("child-1")
    assert root is not None and child is not None
    assert root.authority_usd == Decimal("1.00")
    assert root.consumed_usd == Decimal("0.10")
    assert root.child_reserved_usd == Decimal("0.30")
    assert root.revocation_started_at is None  # migrated in, correctly absent
    assert child.authority_usd == Decimal("0.30")
    assert child.consumed_usd == Decimal("0.10")

    assert store.invariant("root").label == "SATISFIED"
    assert store.invariant("child-1").label == "SATISFIED"


def test_migration_matching_retry_of_pre_migration_event_is_a_safe_no_op(tmp_path):
    db_path = tmp_path / "authority.sqlite3"
    _write_legacy_phase_a_database(db_path)
    store = EconomicAuthorityStore(db_path)

    assert store.consume("evt:c1", "child-1", Decimal("0.10")) is True  # matches original
    child = store.get("child-1")
    assert child is not None
    assert child.consumed_usd == Decimal("0.10")  # not double-counted

    assert (
        store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.30")) == "already_exists"
    )


def test_migration_conflicting_reuse_of_pre_migration_event_raises(tmp_path):
    db_path = tmp_path / "authority.sqlite3"
    _write_legacy_phase_a_database(db_path)
    store = EconomicAuthorityStore(db_path)

    with pytest.raises(EventConflict):
        store.consume("evt:c1", "child-1", Decimal("0.99"))  # different amount than original


def test_migration_survives_real_process_restart(tmp_path):
    """The migration itself is atomic and durable: opening the same
    legacy database repeatedly (as a real restart would) migrates once
    and is a no-op thereafter, never re-applying or losing data."""
    db_path = tmp_path / "authority.sqlite3"
    _write_legacy_phase_a_database(db_path)

    store1 = EconomicAuthorityStore(db_path)
    assert store1.get("root") is not None

    store2 = EconomicAuthorityStore(db_path)  # a fresh instance, same file -- simulates a restart
    root = store2.get("root")
    assert root is not None
    assert root.authority_usd == Decimal("1.00")
    assert root.consumed_usd == Decimal("0.10")

    # New operations against the migrated database work exactly as normal.
    assert store2.reserve("evt:r2", "root", "child-2", "worker", Decimal("0.10")) == "created"


def test_migration_is_a_no_op_against_an_already_current_database(tmp_path):
    """A database created directly by the current code (never legacy) is
    already at CURRENT_SCHEMA_VERSION -- opening it again must not touch
    anything."""
    db_path = tmp_path / "authority.sqlite3"
    store = EconomicAuthorityStore(db_path)
    store.create_root("evt:root", "root", "buyer", Decimal("1.00"))

    store2 = EconomicAuthorityStore(db_path)
    root = store2.get("root")
    assert root is not None
    assert root.authority_usd == Decimal("1.00")


# -- grant conservation: a funded grant draws down the parent (repair item 2) --


def test_grant_against_root_is_a_pure_top_up_with_no_parent_to_fund_it(
    store: EconomicAuthorityStore,
):
    _open_root(store, "1.00")
    assert store.grant("evt:g1", "root", Decimal("5.00")) is True
    root = store.get("root")
    assert root is not None
    assert root.authority_usd == Decimal("6.00")


def test_grant_against_a_child_is_funded_from_parent_headroom(store: EconomicAuthorityStore):
    _open_root(store, "1.00")
    store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.30"))
    # root now has 0.70 active_reservation_usd headroom
    assert store.grant("evt:g1", "child-1", Decimal("0.50")) is True

    child = store.get("child-1")
    root = store.get("root")
    assert child is not None and root is not None
    assert child.authority_usd == Decimal("0.80")  # 0.30 + 0.50
    assert root.child_reserved_usd == Decimal("0.80")  # 0.30 + 0.50, kept in sync
    assert root.active_reservation_usd == Decimal("0.20")  # 1.00 - 0.80


def test_grant_against_a_child_is_rejected_when_parent_headroom_is_insufficient(
    store: EconomicAuthorityStore,
):
    _open_root(store, "1.00")
    store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.30"))
    # root has only 0.70 headroom; requesting 0.71 must be rejected outright,
    # not partially granted or silently capped.
    assert store.grant("evt:g1", "child-1", Decimal("0.71")) is False

    child = store.get("child-1")
    root = store.get("root")
    assert child is not None and root is not None
    assert child.authority_usd == Decimal("0.30")  # untouched
    assert root.child_reserved_usd == Decimal("0.30")  # untouched


def test_grant_against_a_child_is_rejected_when_parent_has_unknown_cost(
    store: EconomicAuthorityStore,
):
    """Unknown cost must never be treated as zero when deciding whether
    more authority is available -- a parent with any unknown-cost event
    cannot fund a grant, even if its known figures look like they have
    headroom."""
    _open_root(store, "1.00")
    store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.30"))
    store.consume("evt:unknown", "root", None)  # root now has unknown cost
    assert store.grant("evt:g1", "child-1", Decimal("0.10")) is False


def test_grant_followed_by_consumption_and_settlement_conserves_correctly(
    store: EconomicAuthorityStore,
):
    """This is the exact scenario the un-funded grant bug broke: without
    parent-side funding, settling a grant-inflated child could drive the
    parent's own child_reserved_usd negative. With funding, it cannot."""
    _open_root(store, "1.00")
    store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.30"))
    store.grant("evt:g1", "child-1", Decimal("0.20"))  # child now has 0.50 authority
    store.consume("evt:c1", "child-1", Decimal("0.40"))
    store.settle("evt:s1", "child-1", "SUCCESS")

    root = store.get("root")
    assert root is not None
    assert root.child_reserved_usd == Decimal("0")  # fully released, never negative
    assert root.consumed_usd == Decimal("0.40")  # child's real spend folded in
    assert root.active_reservation_usd == Decimal("0.60")  # 1.00 - 0.40

    result = store.invariant("root")
    assert result.satisfied_on_known_values is True
    assert result.label == "SATISFIED"


def test_grant_conservation_holds_across_a_three_level_lineage(store: EconomicAuthorityStore):
    _open_root(store, "10.00")
    store.reserve("evt:r1", "root", "child-1", "worker", Decimal("5.00"))
    store.reserve("evt:r2", "child-1", "grandchild-1", "sub-worker", Decimal("2.00"))

    assert store.grant("evt:g1", "grandchild-1", Decimal("1.00")) is True

    grandchild = store.get("grandchild-1")
    child = store.get("child-1")
    root = store.get("root")
    assert grandchild is not None and child is not None and root is not None
    assert grandchild.authority_usd == Decimal("3.00")  # 2.00 + 1.00
    assert child.child_reserved_usd == Decimal("3.00")  # funded the grant
    assert child.active_reservation_usd == Decimal("2.00")  # 5.00 - 3.00
    assert root.child_reserved_usd == Decimal("5.00")  # unaffected -- the grant was funded
    # entirely from child-1's own headroom, never touching root directly

    for delegation_id in ("root", "child-1", "grandchild-1"):
        result = store.invariant(delegation_id)
        assert result.satisfied_on_known_values is True, f"{delegation_id}: {result.detail}"


def test_concurrent_grants_against_the_same_parent_cannot_exceed_its_headroom(tmp_path):
    """Real-thread concurrency proof, mirroring
    test_concurrent_reservations_cannot_exceed_available_authority: two
    grants that would together overrun the parent's headroom must
    serialize so exactly one succeeds."""
    db_path = tmp_path / "authority.sqlite3"
    store = EconomicAuthorityStore(db_path)
    store.create_root("evt:root", "root", "buyer", Decimal("1.00"))
    store.reserve("evt:r1", "root", "child-a", "worker", Decimal("0.10"))
    store.reserve("evt:r2", "root", "child-b", "worker", Decimal("0.10"))
    # root headroom remaining: 0.80

    def try_grant(child_id: str) -> bool:
        return EconomicAuthorityStore(db_path).grant(
            f"evt:grant-{child_id}", child_id, Decimal("0.70")
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(try_grant, "child-a")
        future_b = pool.submit(try_grant, "child-b")
        results = {future_a.result(), future_b.result()}

    assert results == {True, False}, "exactly one of two competing $0.70 grants must succeed"
    root = store.get("root")
    assert root is not None
    assert root.child_reserved_usd == Decimal("0.90")  # 0.10 + 0.10 + exactly one 0.70 grant
    assert root.active_reservation_usd == Decimal("0.10")


# -- decimal normalization and input validation (repair item 8) -----------


def test_equivalent_decimal_amounts_do_not_produce_a_false_conflict(store: EconomicAuthorityStore):
    """"1.0" and "1.00" are the same amount -- a retry expressed with
    different trailing-zero precision must not be mistaken for a
    conflicting reuse of the same event_id."""
    _open_root(store, "1.00")
    assert store.consume("evt:c1", "root", Decimal("0.10")) is True
    assert store.consume("evt:c1", "root", Decimal("0.100")) is True  # same value, different text
    assert store.consume("evt:c1", "root", Decimal("0.1")) is True
    root = store.get("root")
    assert root is not None
    assert root.consumed_usd == Decimal("0.10")  # never double-counted


def test_equivalent_decimal_amounts_do_not_false_conflict_for_reserve(
    store: EconomicAuthorityStore,
):
    _open_root(store, "1.00")
    assert store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.30")) == "created"
    assert (
        store.reserve("evt:r1-retry", "root", "child-1", "worker", Decimal("0.300"))
        == "already_exists"
    )


@pytest.mark.parametrize("bad_amount", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_non_finite_amounts_are_rejected(store: EconomicAuthorityStore, bad_amount: str):
    _open_root(store, "1.00")
    with pytest.raises(ValueError):
        store.grant("evt:g1", "root", Decimal(bad_amount))
    with pytest.raises(ValueError):
        store.consume("evt:c1", "root", Decimal(bad_amount))
    with pytest.raises(ValueError):
        store.reserve("evt:r1", "root", "child-1", "worker", Decimal(bad_amount))
    root = store.get("root")
    assert root is not None
    assert root.authority_usd == Decimal("1.00")  # untouched by any rejected attempt


def test_absurdly_large_amount_is_rejected(store: EconomicAuthorityStore):
    _open_root(store, "1.00")
    with pytest.raises(ValueError):
        store.grant("evt:g1", "root", Decimal("999999999999999999999999"))


@pytest.mark.parametrize("bad_value", ["", "x" * 300])
def test_malformed_identifiers_are_rejected(store: EconomicAuthorityStore, bad_value: str):
    with pytest.raises(ValueError):
        store.create_root("evt:root", bad_value, "buyer", Decimal("1.00"))
    with pytest.raises(ValueError):
        store.create_root(bad_value, "root2", "buyer", Decimal("1.00"))
    with pytest.raises(ValueError):
        store.create_root("evt:root3", "root3", bad_value, Decimal("1.00"))


# -- exclusions: no debug hooks, no outbound calls, no package coupling ---


def test_no_debug_crash_action_or_outbound_network_calls_in_core_module():
    source = (HOSTED_DIR / "core.py").read_text()
    assert "os._exit" not in source
    # A prose mention of "crash-safety" is fine and expected; a literal
    # debug action label (as the private prototype's `action == "crash"`
    # self-destruct hook used) must never appear.
    assert '"crash"' not in source
    for forbidden_import in ("httpx", "requests", "socket", "urllib", "a2a"):
        assert forbidden_import not in source, f"unexpected dependency: {forbidden_import}"

    members = inspect.getmembers(EconomicAuthorityStore, predicate=inspect.isfunction)
    method_names = {name for name, _ in members}
    assert not any("crash" in name.lower() for name in method_names)
    assert not any("delegate_to" in name.lower() for name in method_names), (
        "no method should automatically call out to another agent -- recursive "
        "auto-delegation is explicitly excluded from this phase"
    )


def test_core_module_does_not_import_the_inferrail_package():
    source = (HOSTED_DIR / "core.py").read_text()
    assert "import inferrail" not in source
    assert "from inferrail" not in source


def test_hosted_a2a_economic_authority_is_not_in_the_wheel_build():
    # A real TOML parse of just the wheel's package list -- not a whole-file
    # substring check, which would false-positive on legitimate, unrelated
    # mentions of "hosted/..." paths elsewhere in the file (e.g. a comment
    # documenting an optional dependency, same as
    # hosted/work_economics/requirements.txt already has). Mirrors the same
    # boundary hosted/work_economics/ already keeps (docs/adr/0010).
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        config = tomllib.load(f)
    wheel_packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert not any("hosted" in package for package in wheel_packages)


def test_no_private_strategy_language_in_core_module():
    source = (HOSTED_DIR / "core.py").read_text()
    # Note: the private-strategy-repo name itself is intentionally not
    # listed here as a literal -- doing so would make this file itself
    # match `scripts/check_no_internal_content.sh`'s repo-wide denylist
    # scan for that exact name. That scanner is the authority for
    # excluding the private repo's name; this test only needs to cover
    # phase/decision-record language that wouldn't otherwise be caught.
    forbidden_terms = ("Phase 3", "Phase 1", "research module", "D29", "D30")
    for forbidden_term in forbidden_terms:
        assert forbidden_term not in source
