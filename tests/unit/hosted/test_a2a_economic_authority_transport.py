"""Real A2A transport tests for hosted/a2a_economic_authority/ (Phase B).

Every test that talks to a server starts a real OS subprocess
(`server.py`, running real `uvicorn`) and talks to it with a real,
separately constructed `a2a-sdk` `Client` -- see
`_a2a_economic_authority_client.py`. Nothing here mocks A2A transport,
HTTP, or SQLite.

Deterministic economic-core logic that does not need a live transport
belongs in `test_a2a_economic_authority_core.py`; capability-token logic
that does not need one belongs in
`test_a2a_economic_authority_capabilities.py`. This file exists
specifically to carry the transport-level evidence those unit tests
cannot: real HTTP authentication, real concurrent requests, real process
restart.

Skips automatically unless the hosted extra (a2a-sdk) is installed -- the
same pattern this repo already uses for hosted/work_economics/'s
cdp-sdk/x402-dependent tests (`pip install -e ".[hosted]"`).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTED_DIR = REPO_ROOT / "hosted" / "a2a_economic_authority"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

import pytest  # noqa: E402

pytest.importorskip("a2a")

from _a2a_economic_authority_client import (  # noqa: E402
    agent_process,
    call_context,
    claim_credential,
    free_port,
    make_client,
    send,
)
from a2a.helpers import get_data_parts  # noqa: E402
from a2a.types import TaskState  # noqa: E402
from bootstrap import bootstrap_root  # noqa: E402
from capabilities import CapabilityStore  # noqa: E402
from core import EconomicAuthorityStore  # noqa: E402


@pytest.fixture
def paths(tmp_path: Path):
    return {
        "db": tmp_path / "authority.sqlite3",
        "cap_db": tmp_path / "capabilities.sqlite3",
        "log": tmp_path / "server.log",
        "tmp_path": tmp_path,
    }


@pytest.fixture
def root(paths):
    delegation_id, token = bootstrap_root(
        db_path=paths["db"], capability_db_path=paths["cap_db"], envelope_usd="1.00"
    )
    return delegation_id, token


def _artifact_dict(task, index: int = 0) -> dict:
    return get_data_parts(task.artifacts[index].parts)[0]


# -- happy path: reserve, consume, status, settle -------------------------


async def test_valid_agent_completes_reserve_consume_status_settle(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        reserve_task = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:r1",
                "parent_id": "root",
                "delegation_id": "child-1",
                "agent_id": "worker",
                "maximum_usd": "0.30",
            },
        )
        assert reserve_task.status.state == TaskState.TASK_STATE_COMPLETED
        claim_id = _artifact_dict(reserve_task)["credential_claim_id"]

        claim_response = claim_credential(base_url, claim_id, root_token)
        assert claim_response.status_code == 200
        child_token = claim_response.json()["token"]

        child_client = await make_client(base_url, child_token, session_id="child")
        child_ctx = call_context("child")

        consume_task = await send(
            child_client,
            child_ctx,
            {
                "op": "consume",
                "event_id": "evt:c1",
                "delegation_id": "child-1",
                "amount_usd": "0.10",
            },
        )
        assert consume_task.status.state == TaskState.TASK_STATE_COMPLETED
        assert _artifact_dict(consume_task)["delegation"]["consumed_usd"] == "0.10"

        status_task = await send(
            child_client, child_ctx, {"op": "status", "delegation_id": "child-1"}
        )
        assert status_task.status.state == TaskState.TASK_STATE_COMPLETED
        assert _artifact_dict(status_task)["invariant"]["label"] == "SATISFIED"

        settle_task = await send(
            child_client,
            child_ctx,
            {
                "op": "settle",
                "event_id": "evt:s1",
                "delegation_id": "child-1",
                "outcome": "SUCCESS",
            },
        )
        assert settle_task.status.state == TaskState.TASK_STATE_COMPLETED
        settled = _artifact_dict(settle_task)["delegation"]
        assert settled["active"] is False
        assert settled["outcome"] == "SUCCESS"


# -- missing / invalid credentials -----------------------------------------


async def test_missing_credential_is_rejected(paths, root):
    _root_id, _root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, token=None)
        ctx = call_context()
        task = await send(client, ctx, {"op": "status", "delegation_id": "root"})
        assert task.status.state == TaskState.TASK_STATE_REJECTED
        assert not task.artifacts  # no economic data ever released without a credential


async def test_invalid_credential_is_rejected(paths, root):
    _root_id, _root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, token="not-a-real-token-at-all")
        ctx = call_context()
        task = await send(client, ctx, {"op": "status", "delegation_id": "root"})
        assert task.status.state == TaskState.TASK_STATE_REJECTED


# -- a token for one delegation cannot control another ---------------------


async def test_token_for_one_delegation_cannot_control_another(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        reserve_task = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:r1",
                "parent_id": "root",
                "delegation_id": "child-1",
                "agent_id": "worker",
                "maximum_usd": "0.30",
            },
        )
        claim_id = _artifact_dict(reserve_task)["credential_claim_id"]
        child_token = claim_credential(base_url, claim_id, root_token).json()["token"]

        reserve_task2 = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:r2",
                "parent_id": "root",
                "delegation_id": "child-2",
                "agent_id": "worker",
                "maximum_usd": "0.30",
            },
        )
        claim_id2 = _artifact_dict(reserve_task2)["credential_claim_id"]
        other_child_token = claim_credential(base_url, claim_id2, root_token).json()["token"]

        child_client = await make_client(base_url, child_token, session_id="child")
        child_ctx = call_context("child")

        # child-1's token must not be able to consume against child-2.
        cross_task = await send(
            child_client,
            child_ctx,
            {
                "op": "consume",
                "event_id": "evt:cross",
                "delegation_id": "child-2",
                "amount_usd": "0.01",
            },
        )
        assert cross_task.status.state == TaskState.TASK_STATE_REJECTED

        # It still works against its own delegation.
        own_task = await send(
            child_client,
            child_ctx,
            {
                "op": "consume",
                "event_id": "evt:own",
                "delegation_id": "child-1",
                "amount_usd": "0.01",
            },
        )
        assert own_task.status.state == TaskState.TASK_STATE_COMPLETED

        assert other_child_token  # sanity: the other token really was minted


# -- scope enforcement -------------------------------------------------


async def test_scopes_are_enforced_per_operation(paths, root):
    """A child token minted with the default narrow scopes (read, consume,
    settle) must be rejected for reserve/grant/revoke, and must succeed for
    read/consume/settle."""
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()
        reserve_task = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:r1",
                "parent_id": "root",
                "delegation_id": "child-1",
                "agent_id": "worker",
                "maximum_usd": "0.30",
            },
        )
        claim_id = _artifact_dict(reserve_task)["credential_claim_id"]
        child_token = claim_credential(base_url, claim_id, root_token).json()["token"]

        child_client = await make_client(base_url, child_token, session_id="child")
        child_ctx = call_context("child")

        narrow_scope_ops = [
            (
                "reserve",
                {
                    "op": "reserve",
                    "event_id": "evt:x1",
                    "parent_id": "child-1",
                    "delegation_id": "grandchild",
                    "agent_id": "sub",
                    "maximum_usd": "0.01",
                },
            ),
            (
                "grant",
                {
                    "op": "grant",
                    "event_id": "evt:x2",
                    "delegation_id": "child-1",
                    "amount_usd": "0.10",
                },
            ),
            ("revoke", {"op": "revoke", "event_id": "evt:x3", "delegation_id": "child-1"}),
        ]
        for op, payload in narrow_scope_ops:
            task = await send(child_client, child_ctx, payload)
            assert task.status.state == TaskState.TASK_STATE_REJECTED, (
                f"{op} should have been rejected for a narrow-scope child token"
            )

        granted_scope_ops = [
            ("read", {"op": "status", "delegation_id": "child-1"}),
            (
                "consume",
                {
                    "op": "consume",
                    "event_id": "evt:x4",
                    "delegation_id": "child-1",
                    "amount_usd": "0.05",
                },
            ),
        ]
        for op, payload in granted_scope_ops:
            task = await send(child_client, child_ctx, payload)
            assert task.status.state == TaskState.TASK_STATE_COMPLETED, (
                f"{op} should have succeeded for its granted scope"
            )

        settle_task = await send(
            child_client,
            child_ctx,
            {
                "op": "settle",
                "event_id": "evt:x5",
                "delegation_id": "child-1",
                "outcome": "SUCCESS",
            },
        )
        assert settle_task.status.state == TaskState.TASK_STATE_COMPLETED


# -- expired and revoked tokens ---------------------------------------


async def test_expired_token_fails_over_transport(paths):
    delegation_id, _token = bootstrap_root(
        db_path=paths["db"], capability_db_path=paths["cap_db"], envelope_usd="1.00"
    )
    # Mint a second, already-expired token directly against the same capability store.
    cap_store = CapabilityStore(paths["cap_db"])
    _token_id, expired_token = cap_store.issue(delegation_id, {"read"}, ttl_seconds=-1)

    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, expired_token)
        ctx = call_context()
        task = await send(client, ctx, {"op": "status", "delegation_id": "root"})
        assert task.status.state == TaskState.TASK_STATE_REJECTED


async def test_revoked_token_fails_over_transport(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        revoke_task = await send(
            client, ctx, {"op": "revoke", "event_id": "evt:selfrevoke", "delegation_id": "root"}
        )
        assert revoke_task.status.state == TaskState.TASK_STATE_COMPLETED

        after_task = await send(client, ctx, {"op": "status", "delegation_id": "root"})
        assert after_task.status.state == TaskState.TASK_STATE_REJECTED


# -- revoking root invalidates the whole descendant tree ------------------


async def test_revoking_root_invalidates_all_descendant_capabilities(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        reserve1 = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:r1",
                "parent_id": "root",
                "delegation_id": "child-1",
                "agent_id": "worker",
                "maximum_usd": "0.60",
            },
        )
        claim_id1 = _artifact_dict(reserve1)["credential_claim_id"]
        child_token = claim_credential(base_url, claim_id1, root_token).json()["token"]

        # child-2 is reserved with widened child_scopes so it can itself
        # reserve a grandchild -- the default narrow scopes (read/consume/
        # settle) deliberately do not include reserve.
        reserve_wide = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:rw",
                "parent_id": "root",
                "delegation_id": "child-2",
                "agent_id": "worker",
                "maximum_usd": "0.30",
                "child_scopes": ["read", "reserve", "consume", "settle"],
            },
        )
        claim_id2 = _artifact_dict(reserve_wide)["credential_claim_id"]
        child2_token = claim_credential(base_url, claim_id2, root_token).json()["token"]

        child2_client = await make_client(base_url, child2_token, session_id="child2")
        child2_ctx = call_context("child2")
        reserve_grandchild = await send(
            child2_client,
            child2_ctx,
            {
                "op": "reserve",
                "event_id": "evt:rg",
                "parent_id": "child-2",
                "delegation_id": "grandchild-1",
                "agent_id": "sub",
                "maximum_usd": "0.10",
            },
        )
        claim_id3 = _artifact_dict(reserve_grandchild)["credential_claim_id"]
        grandchild_token = claim_credential(base_url, claim_id3, child2_token).json()["token"]

        revoke_task = await send(
            client, ctx, {"op": "revoke", "event_id": "evt:revoke-root", "delegation_id": "root"}
        )
        assert revoke_task.status.state == TaskState.TASK_STATE_COMPLETED
        artifact = _artifact_dict(revoke_task)
        revoked_ids = {"root", "child-1", "child-2", "grandchild-1"}
        assert set(artifact["revoked_delegation_ids"]) == revoked_ids

        probes = [
            (root_token, "root", "s1"),
            (child_token, "child-1", "s2"),
            (child2_token, "child-2", "s3"),
            (grandchild_token, "grandchild-1", "s4"),
        ]
        for token, delegation_id, session in probes:
            probe_client = await make_client(base_url, token, session_id=session)
            probe_ctx = call_context(session)
            probe_task = await send(
                probe_client, probe_ctx, {"op": "status", "delegation_id": delegation_id}
            )
            assert probe_task.status.state == TaskState.TASK_STATE_REJECTED, (
                f"{delegation_id}'s token should be revoked"
            )


# -- credentials never leak into task history, artifacts, or logs ---------


async def test_tokens_never_appear_in_task_history_or_logs(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        reserve_task = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:r1",
                "parent_id": "root",
                "delegation_id": "child-1",
                "agent_id": "worker",
                "maximum_usd": "0.30",
            },
        )
        claim_id = _artifact_dict(reserve_task)["credential_claim_id"]
        child_token = claim_credential(base_url, claim_id, root_token).json()["token"]

        child_client = await make_client(base_url, child_token, session_id="child")
        child_ctx = call_context("child")
        await send(
            child_client,
            child_ctx,
            {
                "op": "consume",
                "event_id": "evt:c1",
                "delegation_id": "child-1",
                "amount_usd": "0.10",
            },
        )
        await send(
            child_client,
            child_ctx,
            {
                "op": "settle",
                "event_id": "evt:s1",
                "delegation_id": "child-1",
                "outcome": "SUCCESS",
            },
        )

        serialized_task = str(reserve_task)
        assert root_token not in serialized_task
        assert child_token not in serialized_task

    log_text = paths["log"].read_text()
    assert root_token not in log_text
    assert child_token not in log_text


# -- duplicate delivery: one economic effect -------------------------------


async def test_duplicate_delivery_causes_one_economic_effect(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()
        payload = {
            "op": "reserve",
            "event_id": "evt:dup",
            "parent_id": "root",
            "delegation_id": "child-dup",
            "agent_id": "worker",
            "maximum_usd": "0.10",
        }

        first = await send(client, ctx, payload)
        # a genuine duplicate delivery: new HTTP call, same event_id
        second = await send(client, ctx, payload)
        assert first.status.state == TaskState.TASK_STATE_COMPLETED
        assert second.status.state == TaskState.TASK_STATE_COMPLETED

    core_store = EconomicAuthorityStore(paths["db"])
    root_state = core_store.get("root")
    assert root_state is not None
    assert root_state.child_reserved_usd == Decimal("0.10"), (
        "a duplicate delivery must not reserve twice"
    )


# -- concurrency: competing reservations cannot exceed authority -----------


async def test_concurrent_reservations_cannot_exceed_authority_over_transport(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client_a = await make_client(base_url, root_token, session_id="a")
        client_b = await make_client(base_url, root_token, session_id="b")

        payload_a = {
            "op": "reserve",
            "event_id": "evt:ra",
            "parent_id": "root",
            "delegation_id": "child-a",
            "agent_id": "worker",
            "maximum_usd": "0.70",
        }
        payload_b = {
            "op": "reserve",
            "event_id": "evt:rb",
            "parent_id": "root",
            "delegation_id": "child-b",
            "agent_id": "worker",
            "maximum_usd": "0.70",
        }
        results = await asyncio.gather(
            send(client_a, call_context("a"), payload_a),
            send(client_b, call_context("b"), payload_b),
        )
        states = {task.status.state for task in results}

    # Both requests are individually valid against the root's $1.00 authority,
    # but together they would overrun it ($1.40 > $1.00) -- exactly one of
    # the two underlying reservations must have actually gone through.
    core_store = EconomicAuthorityStore(paths["db"])
    root_state = core_store.get("root")
    assert root_state is not None
    assert root_state.child_reserved_usd == Decimal("0.70")
    # Exactly one request completes. The other observes insufficient
    # headroom -- reported as FAILED if it lost the race inside the
    # atomic reserve itself, or AUTH_REQUIRED if it computed the shortfall
    # from a snapshot taken after the winner had already committed. Either
    # is a correct, timing-dependent outcome; TASK_STATE_COMPLETED must
    # never appear twice.
    assert TaskState.TASK_STATE_COMPLETED in states
    assert states != {TaskState.TASK_STATE_COMPLETED}
    possible_loser_states = {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_AUTH_REQUIRED,
    }
    assert states <= possible_loser_states


# -- state and revocation survive real process restart ---------------------


async def test_state_and_revocation_survive_process_restart(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()
        reserve_task = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:r1",
                "parent_id": "root",
                "delegation_id": "child-1",
                "agent_id": "worker",
                "maximum_usd": "0.30",
            },
        )
        claim_id = _artifact_dict(reserve_task)["credential_claim_id"]
        child_token = claim_credential(base_url, claim_id, root_token).json()["token"]

        child_client = await make_client(base_url, child_token, session_id="child")
        await send(
            child_client,
            call_context("child"),
            {
                "op": "consume",
                "event_id": "evt:c1",
                "delegation_id": "child-1",
                "amount_usd": "0.10",
            },
        )

        revoke_task = await send(
            client,
            ctx,
            {"op": "revoke", "event_id": "evt:revoke-child", "delegation_id": "child-1"},
        )
        assert revoke_task.status.state == TaskState.TASK_STATE_COMPLETED
    # Exiting the `with` block above terminates the subprocess -- a real
    # shutdown, from the app's own perspective an uncontrolled one.

    port2 = free_port()
    with agent_process(port2, paths["db"], paths["cap_db"], paths["log"]) as base_url2:
        fresh_client = await make_client(base_url2, root_token, session_id="fresh")
        status_task = await send(
            fresh_client, call_context("fresh"), {"op": "status", "delegation_id": "root"}
        )
        assert status_task.status.state == TaskState.TASK_STATE_COMPLETED
        root_state = _artifact_dict(status_task)["delegation"]
        # child's consumption folded in by the settle inside revoke
        assert root_state["consumed_usd"] == "0.10"

        revoked_child_client = await make_client(base_url2, child_token, session_id="revoked-child")
        revoked_task = await send(
            revoked_child_client,
            call_context("revoked-child"),
            {"op": "status", "delegation_id": "child-1"},
        )
        assert revoked_task.status.state == TaskState.TASK_STATE_REJECTED, (
            "revocation must survive the restart"
        )


# -- unknown cost stays explicitly uncertain over real transport -----------


async def test_unknown_cost_remains_explicitly_uncertain_over_transport(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()
        task = await send(
            client,
            ctx,
            {
                "op": "consume",
                "event_id": "evt:unknown",
                "delegation_id": "root",
                "amount_usd": None,
            },
        )
        assert task.status.state == TaskState.TASK_STATE_COMPLETED
        receipt = _artifact_dict(task)
        assert receipt["delegation"]["unknown_cost_count"] == 1.0
        assert receipt["invariant"]["certainty"] == "PARTIAL"
        assert receipt["invariant"]["label"] == "NOT VIOLATED ON KNOWN VALUES (PARTIAL)"


# -- authorization-required -> grant -> retry, over real transport --------


async def test_authorization_required_grant_retry_flow(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        parked = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:overrun",
                "parent_id": "root",
                "delegation_id": "child-overrun",
                "agent_id": "worker",
                "maximum_usd": "5.00",
            },
        )
        assert parked.status.state == TaskState.TASK_STATE_AUTH_REQUIRED
        assert get_data_parts(parked.status.message.parts)[0]["shortfall_usd"] == "4.00"

        # Without a grant, the reservation never happened.
        core_store = EconomicAuthorityStore(paths["db"])
        assert core_store.get("child-overrun") is None

        retried = await send(
            client,
            ctx,
            {
                "op": "grant",
                "event_id": "evt:grant1",
                "delegation_id": "root",
                "amount_usd": "4.00",
            },
            task_id=parked.id,
        )
        assert retried.status.state == TaskState.TASK_STATE_COMPLETED
        # `retried.artifacts` now holds both the original "pending-operation"
        # artifact (from the park) and the new receipt from the completed
        # retry -- the receipt is the last one appended.
        assert _artifact_dict(retried, index=-1)["delegation_id"] == "child-overrun"
        assert core_store.get("child-overrun") is not None


# -- no automatic outbound agent calls --------------------------------------


def test_no_outbound_calls_in_executor_and_server_modules():
    for filename in ("executor.py", "server.py", "agent_card.py", "bootstrap.py"):
        source = (HOSTED_DIR / filename).read_text()
        for forbidden_import in ("requests", "socket", "urllib3"):
            assert forbidden_import not in source, (
                f"{filename} has unexpected dependency: {forbidden_import}"
            )
        assert "delegate_to" not in source.lower(), (
            f"{filename} must not automatically call another agent"
        )


# -- Work Economics is completely unchanged ---------------------------------


def test_work_economics_is_untouched():
    watched_paths = [
        "hosted/work_economics",
        "docs/capabilities",
        "examples/work_economics_purchase.py",
    ]
    diff = subprocess.run(
        ["git", "diff", "--stat", "origin/main", "--", *watched_paths],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert diff.stdout.strip() == "", (
        f"Work Economics must remain untouched by Phase B:\n{diff.stdout}"
    )


# -- boundary scanner stays clean --------------------------------------


def test_boundary_scanner_reports_clean():
    result = subprocess.run(
        ["bash", "scripts/check_no_internal_content.sh"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
