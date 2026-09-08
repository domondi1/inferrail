"""Tests for hosted/a2a_economic_authority/core.py's durable economic-authority
core. No network, no secrets, no A2A/x402 dependency -- this module is
transport-independent and so are these tests.
"""

from __future__ import annotations

import inspect
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
from core import EconomicAuthorityStore  # noqa: E402

CRASH_HELPER = Path(__file__).resolve().parent / "_a2a_economic_authority_crash_helper.py"


@pytest.fixture
def store(tmp_path) -> EconomicAuthorityStore:
    return EconomicAuthorityStore(tmp_path / "authority.sqlite3")


def _open_root(store: EconomicAuthorityStore, authority_usd: str = "1.00") -> None:
    store.create_root("evt:root", "root", "buyer", Decimal(authority_usd))


# -- basic lifecycle --------------------------------------------------


def test_reserve_consume_settle_happy_path(store: EconomicAuthorityStore):
    _open_root(store)
    assert store.reserve("evt:reserve", "root", "child-1", "worker", Decimal("0.30")) is True
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
    assert store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.60")) is True
    # Only $0.40 headroom remains; a $0.60 reservation must be denied, not
    # partially granted or silently capped.
    assert store.reserve("evt:r2", "root", "child-2", "worker", Decimal("0.60")) is False
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

    def try_reserve(child_id: str) -> bool:
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

    assert results == {True, False}, "exactly one of two competing $0.70 reservations must succeed"
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
    assert store.reserve("evt:r1", "root", "child-1", "worker", Decimal("0.50")) is True
    # A second reserve call naming the same delegation_id -- even with a
    # different event_id, as a duplicate transport delivery would produce
    # -- must not re-reserve parent headroom.
    assert store.reserve("evt:r2-different", "root", "child-1", "worker", Decimal("0.50")) is True
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
