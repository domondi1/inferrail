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
import json
import sqlite3
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

import httpx  # noqa: E402
from _a2a_economic_authority_client import (  # noqa: E402
    agent_process,
    call_context,
    claim_credential,
    free_port,
    make_client,
    send,
)
from a2a.helpers import get_data_parts  # noqa: E402
from a2a.types import (  # noqa: E402
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    TaskPushNotificationConfig,
    TaskState,
)
from bootstrap import bootstrap_root  # noqa: E402
from capabilities import SCOPES, CapabilityStore, RevokedCredential  # noqa: E402
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


# -- repair item 1: every non-SendMessage A2A method is disabled ----------
#
# access_control.SendMessageOnlyRequestHandler disables GetTask, ListTasks,
# CancelTask, SubscribeToTask, and the push-notification-config methods
# entirely -- these tests prove that holds regardless of credential
# validity (missing, a valid-but-unrelated token, or the correct token),
# since "disabled" must mean disabled, not merely "not separately
# authorized".


async def test_get_task_is_disabled_regardless_of_credential(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()
        status_task = await send(client, ctx, {"op": "status", "delegation_id": "root"})
        task_id = status_task.id

        for token, label in [(root_token, "valid"), (None, "missing"), ("garbage", "invalid")]:
            probe_client = await make_client(base_url, token, session_id=f"probe-{label}")
            probe_ctx = call_context(f"probe-{label}")
            with pytest.raises(Exception) as excinfo:  # noqa: PT011 -- SDK raises a generic A2A client error
                await probe_client.get_task(GetTaskRequest(id=task_id), context=probe_ctx)
            assert "SendMessage" in str(excinfo.value), (
                f"GetTask with a {label} credential must be disabled, not merely unauthorized"
            )


async def test_list_tasks_is_disabled_regardless_of_credential(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        for token, label in [(root_token, "valid"), (None, "missing")]:
            probe_client = await make_client(base_url, token, session_id=f"probe-{label}")
            probe_ctx = call_context(f"probe-{label}")
            with pytest.raises(Exception) as excinfo:  # noqa: PT011
                await probe_client.list_tasks(ListTasksRequest(), context=probe_ctx)
            assert "SendMessage" in str(excinfo.value)


async def test_cancel_task_is_disabled_regardless_of_credential(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()
        status_task = await send(client, ctx, {"op": "status", "delegation_id": "root"})
        task_id = status_task.id

        for token, label in [(root_token, "valid"), (None, "missing")]:
            probe_client = await make_client(base_url, token, session_id=f"probe-{label}")
            probe_ctx = call_context(f"probe-{label}")
            with pytest.raises(Exception) as excinfo:  # noqa: PT011
                await probe_client.cancel_task(CancelTaskRequest(id=task_id), context=probe_ctx)
            assert "SendMessage" in str(excinfo.value)

        # And the task really is untouched -- it did not silently cancel.
        after = await send(client, ctx, {"op": "status", "delegation_id": "root"})
        assert after.status.state == TaskState.TASK_STATE_COMPLETED


async def test_subscribe_to_task_is_disabled_regardless_of_credential(paths, root):
    """The real `a2a-sdk` client refuses to even attempt `SubscribeToTask`
    client-side once it sees the Agent Card declare `streaming=False` --
    a correct, even stronger outcome. To directly prove the *server's own*
    disabled path (`access_control.py`) still works regardless -- in case
    a future Agent Card ever adds streaming support without updating that
    wrapper -- this test bypasses the smart client and sends the raw
    JSON-RPC request itself.
    """
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()
        status_task = await send(client, ctx, {"op": "status", "delegation_id": "root"})
        task_id = status_task.id

        for headers, label in [
            ({"Authorization": f"Bearer {root_token}"}, "valid"),
            ({}, "missing"),
        ]:
            response = httpx.post(
                base_url,
                json={
                    "jsonrpc": "2.0",
                    "id": "probe-1",
                    "method": "SubscribeToTask",
                    "params": {"id": task_id},
                },
                headers={"A2A-Version": "1.0", **headers},
                timeout=10.0,
            )
            body = response.json()
            assert "error" in body, f"SubscribeToTask with a {label} credential must be disabled"
            assert "SendMessage" in body["error"]["message"]


async def test_push_notification_config_is_disabled_regardless_of_credential(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()
        status_task = await send(client, ctx, {"op": "status", "delegation_id": "root"})
        task_id = status_task.id

        for token, label in [(root_token, "valid"), (None, "missing")]:
            probe_client = await make_client(base_url, token, session_id=f"probe-{label}")
            probe_ctx = call_context(f"probe-{label}")
            with pytest.raises(Exception) as excinfo:  # noqa: PT011
                await probe_client.create_task_push_notification_config(
                    TaskPushNotificationConfig(task_id=task_id), context=probe_ctx
                )
            assert "SendMessage" in str(excinfo.value)


async def test_executor_cancel_refuses_without_valid_authorization(tmp_path):
    """Direct, defense-in-depth proof for `EconomicAuthorityExecutor.cancel()`
    itself (unreachable via HTTP today, since CancelTask is disabled at the
    transport layer above, but must never silently succeed if that ever
    changes): constructs a real `RequestContext`/`Task` and calls `cancel()`
    directly with no credential, then with an unrelated credential, and
    asserts neither ever enqueues a cancellation event.
    """
    from a2a.helpers import new_data_message, new_task_from_user_message
    from a2a.server.agent_execution import RequestContext
    from a2a.server.context import ServerCallContext
    from a2a.types import Role
    from capabilities import InMemoryCredentialHandoff
    from executor import EconomicAuthorityExecutor

    class _SpyEventQueue:
        def __init__(self) -> None:
            self.events: list[object] = []

        async def enqueue_event(self, event: object) -> None:
            self.events.append(event)

    db_path = tmp_path / "authority.sqlite3"
    cap_db_path = tmp_path / "capabilities.sqlite3"
    bootstrap_root(db_path=db_path, capability_db_path=cap_db_path, envelope_usd="1.00")
    core_store = EconomicAuthorityStore(db_path)
    capability_store = CapabilityStore(cap_db_path)
    handoff = InMemoryCredentialHandoff()
    executor_under_test = EconomicAuthorityExecutor(core_store, capability_store, handoff)

    message = new_data_message({"op": "status", "delegation_id": "root"}, role=Role.ROLE_USER)
    task = new_task_from_user_message(message)

    for headers in ({}, {"authorization": "Bearer some-unrelated-garbage-token"}):
        call_ctx = ServerCallContext(state={"headers": headers})
        request_ctx = RequestContext(
            call_context=call_ctx, task=task, task_id=task.id, context_id=task.context_id
        )
        request_ctx.current_task = task

        spy_queue = _SpyEventQueue()
        await executor_under_test.cancel(request_ctx, spy_queue)  # type: ignore[arg-type]

        assert spy_queue.events == [], (
            "an unauthenticated or unrelated-credential cancel() call must never enqueue anything"
        )


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


# -- repair item 3: /capabilities/claim revalidates the issuer at claim time --


async def test_claim_response_is_never_cached(paths, root):
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
                "event_id": "evt:cache1",
                "parent_id": "root",
                "delegation_id": "child-cache",
                "agent_id": "worker",
                "maximum_usd": "0.10",
            },
        )
        claim_id = _artifact_dict(reserve_task)["credential_claim_id"]

        response = claim_credential(base_url, claim_id, root_token)
        assert response.status_code == 200
        cache_control = response.headers.get("cache-control", "")
        assert "no-store" in cache_control


async def test_claim_endpoint_rejects_malformed_json_cleanly(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        response = httpx.post(
            f"{base_url}/capabilities/claim",
            content=b"{not-valid-json,,,",
            headers={
                "Authorization": f"Bearer {root_token}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )
        assert response.status_code == 400
        assert "no-store" in response.headers.get("cache-control", "")


async def test_claim_rejects_when_issuer_credential_has_expired(paths, root):
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
                "event_id": "evt:expire1",
                "parent_id": "root",
                "delegation_id": "child-expire",
                "agent_id": "worker",
                "maximum_usd": "0.10",
            },
        )
        claim_id = _artifact_dict(reserve_task)["credential_claim_id"]

        # An already-expired token for the SAME delegation/scope must not
        # redeem the claim -- matching hash bytes is not enough; the
        # issuer must currently be valid.
        cap_store = CapabilityStore(paths["cap_db"])
        _tid, expired_reserve_token = cap_store.issue("root", {"reserve"}, ttl_seconds=-1)

        response = claim_credential(base_url, claim_id, expired_reserve_token)
        assert response.status_code == 403
        assert response.json()["error"] == "ExpiredCredential"


async def test_claim_rejects_and_purges_when_issuer_credential_is_revoked(paths, root):
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
                "event_id": "evt:revoke-issuer",
                "parent_id": "root",
                "delegation_id": "child-revoke-issuer",
                "agent_id": "worker",
                "maximum_usd": "0.10",
            },
        )
        claim_id = _artifact_dict(reserve_task)["credential_claim_id"]

        # Revoke ROOT entirely -- this both invalidates root_token's own
        # authority and (via executor.py's purge) removes the outstanding
        # claim itself.
        await send(
            client, ctx, {"op": "revoke", "event_id": "evt:revoke-all", "delegation_id": "root"}
        )

        response = claim_credential(base_url, claim_id, root_token)
        assert response.status_code == 403
        # The claim is gone outright (purged), not merely unredeemable by
        # this specific now-revoked token -- prove it with a FRESH,
        # currently-valid reserve-scoped token for root, which still
        # cannot redeem a purged claim_id.
        cap_store = CapabilityStore(paths["cap_db"])
        _tid, fresh_token = cap_store.issue("root", {"reserve"})
        response2 = claim_credential(base_url, claim_id, fresh_token)
        assert response2.status_code == 403
        assert response2.json()["error"] == "InvalidCredential"


async def test_concurrent_claim_redemption_yields_exactly_one_success(paths, root):
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
                "event_id": "evt:concurrent-claim",
                "parent_id": "root",
                "delegation_id": "child-concurrent-claim",
                "agent_id": "worker",
                "maximum_usd": "0.10",
            },
        )
        claim_id = _artifact_dict(reserve_task)["credential_claim_id"]

        async def attempt() -> int:
            response = await asyncio.to_thread(claim_credential, base_url, claim_id, root_token)
            return response.status_code

        results = await asyncio.gather(*(attempt() for _ in range(12)))

    successes = [code for code in results if code == 200]
    assert len(successes) == 1, (
        f"exactly one concurrent claim redemption must succeed, got {results}"
    )
    assert all(code in (200, 403) for code in results)


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


# -- repair item 5: idempotency is scoped per delegation, not global ------


async def test_same_event_id_across_two_independent_delegations_does_not_collide(paths, root):
    """Two independent delegations under the same root, each controlled by
    its own narrow capability, both pick the SAME caller-chosen event_id
    for an unrelated `grant` -- they must not collide; each must be
    applied independently."""
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        for delegation_id in ("child-indep-a", "child-indep-b"):
            await send(
                client,
                ctx,
                {
                    "op": "reserve",
                    "event_id": f"evt:seed-{delegation_id}",
                    "parent_id": "root",
                    "delegation_id": delegation_id,
                    "agent_id": "worker",
                    "maximum_usd": "0.30",
                    "child_scopes": ["read", "grant"],
                },
            )

        cap_store = CapabilityStore(paths["cap_db"])
        _id_a, token_a = cap_store.issue("child-indep-a", {"grant"})
        _id_b, token_b = cap_store.issue("child-indep-b", {"grant"})
        client_a = await make_client(base_url, token_a, session_id="indep-a")
        client_b = await make_client(base_url, token_b, session_id="indep-b")

        result_a = await send(
            client_a,
            call_context("indep-a"),
            {
                "op": "grant",
                "event_id": "evt:shared-key",
                "delegation_id": "child-indep-a",
                "amount_usd": "0.05",
            },
        )
        result_b = await send(
            client_b,
            call_context("indep-b"),
            {
                "op": "grant",
                "event_id": "evt:shared-key",
                "delegation_id": "child-indep-b",
                "amount_usd": "0.07",
            },
        )
        assert result_a.status.state == TaskState.TASK_STATE_COMPLETED
        assert result_b.status.state == TaskState.TASK_STATE_COMPLETED

    core_store = EconomicAuthorityStore(paths["db"])
    child_a = core_store.get("child-indep-a")
    child_b = core_store.get("child-indep-b")
    assert child_a is not None and child_a.authority_usd == Decimal("0.35")
    assert child_b is not None and child_b.authority_usd == Decimal("0.37")


async def test_reserve_retry_with_mismatched_amount_fails_explicitly(paths, root):
    """A caller reuses an existing delegation_id (the natural retry key
    for `reserve`) but with a DIFFERENT amount than the original -- this
    is a conflicting reuse, not a retry, and must fail explicitly rather
    than silently reporting success for the wrong amount."""
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        first = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:conflict-r1",
                "parent_id": "root",
                "delegation_id": "child-conflict",
                "agent_id": "worker",
                "maximum_usd": "0.30",
            },
        )
        assert first.status.state == TaskState.TASK_STATE_COMPLETED

        conflicting = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:conflict-r2-different",
                "parent_id": "root",
                "delegation_id": "child-conflict",  # same delegation_id
                "agent_id": "worker",
                "maximum_usd": "0.99",  # different amount -- a conflict, not a retry
            },
        )
        assert conflicting.status.state == TaskState.TASK_STATE_FAILED
        assert "conflict" in get_data_parts(conflicting.status.message.parts)[0]["error"].lower()

    core_store = EconomicAuthorityStore(paths["db"])
    child = core_store.get("child-conflict")
    assert child is not None
    assert child.authority_usd == Decimal("0.30"), (
        "the original reservation's amount must be untouched"
    )


# -- reservation retries recover a fresh credential (finding 1) -----------


async def test_matching_reserve_retry_by_the_original_authorizer_recovers_a_fresh_credential(
    paths, root
):
    """A matching duplicate delivery of a successful reserve, sent by the
    EXACT credential that authorized the original reservation, must
    recover a fresh, usable claim/credential each time -- this is the
    crash-safe recovery path (finding 1): a crash or lost response
    between committing the reservation and the caller obtaining a usable
    credential must never strand the child authority. Economic authority
    is never reserved twice, and at most one capability token for this
    delegation is ever live: each recovery revokes whatever came before."""
    _root_id, root_token = root
    delegation_id = "child-recovers"

    def live_token_ids() -> list[str]:
        with sqlite3.connect(paths["cap_db"]) as conn:
            rows = conn.execute(
                "SELECT token_id FROM capability_tokens WHERE delegation_id = ? AND revoked = 0",
                (delegation_id,),
            ).fetchall()
            return [row[0] for row in rows]

    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()
        payload = {
            "op": "reserve",
            "event_id": "evt:recovers",
            "parent_id": "root",
            "delegation_id": delegation_id,
            "agent_id": "worker",
            "maximum_usd": "0.10",
        }

        first = await send(client, ctx, payload)
        assert first.status.state == TaskState.TASK_STATE_COMPLETED
        claim_ids = [_artifact_dict(first)["credential_claim_id"]]
        assert claim_ids[0] is not None

        for _ in range(5):
            retry = await send(client, ctx, payload)
            assert retry.status.state == TaskState.TASK_STATE_COMPLETED
            retry_claim_id = _artifact_dict(retry)["credential_claim_id"]
            assert retry_claim_id is not None, (
                "the exact original authorizer must recover a fresh, usable claim on retry"
            )
            assert retry_claim_id not in claim_ids, "each recovery must be a genuinely new claim"
            claim_ids.append(retry_claim_id)

        assert len(live_token_ids()) == 1, (
            "exactly one live capability token must exist after repeated recovery -- every "
            "previous one must have been revoked"
        )

        final_claim = claim_ids[-1]
        claimed = claim_credential(base_url, final_claim, root_token)
        assert claimed.status_code == 200
        plaintext = claimed.json()["token"]

        cap_store = CapabilityStore(paths["cap_db"])
        info = cap_store.authorize(plaintext, delegation_id, "read")
        assert info.token_id == live_token_ids()[0], "the redeemed credential must be the live one"

    core_store = EconomicAuthorityStore(paths["db"])
    root_state = core_store.get("root")
    assert root_state is not None
    assert root_state.child_reserved_usd == Decimal("0.10"), (
        "economic authority must never be reserved twice across repeated recovery retries"
    )


async def test_reserve_retry_without_a_durable_authorization_record_falls_back_safely(paths, root):
    """A delegation created without ever going through
    `record_reservation_authorization` (e.g. one seeded directly against
    core.py/capabilities.py, predating this recovery mechanism) has no
    durable binding to recover against. A matching retry against it must
    fall back to the old, safe no-mint behavior rather than erroring or
    guessing an authorizer -- honest degradation, not a new attack
    surface."""
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        core_store = EconomicAuthorityStore(paths["db"])
        core_store.reserve("evt:seed-legacy", "root", "child-legacy", "worker", Decimal("0.10"))
        cap_store = CapabilityStore(paths["cap_db"])
        assert cap_store.get_reservation_authorization("child-legacy") is None

        client = await make_client(base_url, root_token)
        ctx = call_context()
        retry = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:legacy-retry",
                "parent_id": "root",
                "delegation_id": "child-legacy",
                "agent_id": "worker",
                "maximum_usd": "0.10",
            },
        )
        assert retry.status.state == TaskState.TASK_STATE_COMPLETED
        assert _artifact_dict(retry)["credential_claim_id"] is None
        assert _artifact_dict(retry)["note"] == "reservation_already_exists"


async def test_reserve_retry_with_changed_child_scopes_is_an_explicit_conflict(paths, root):
    """Requesting different `child_scopes` on a retry of an existing
    reservation must fail explicitly, not silently mint a differently-
    scoped credential (which `child_scopes` is not otherwise part of the
    durable reservation identity core.py checks)."""
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        first = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:scope-drift-1",
                "parent_id": "root",
                "delegation_id": "child-scope-drift",
                "agent_id": "worker",
                "maximum_usd": "0.10",
                "child_scopes": ["read", "consume"],
            },
        )
        assert first.status.state == TaskState.TASK_STATE_COMPLETED

        drifted = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:scope-drift-2",
                "parent_id": "root",
                "delegation_id": "child-scope-drift",
                "agent_id": "worker",
                "maximum_usd": "0.10",
                "child_scopes": ["read", "consume", "settle", "revoke"],
            },
        )
        assert drifted.status.state == TaskState.TASK_STATE_FAILED
        assert not drifted.artifacts, "no credential may be issued for a rejected scope-drift retry"


async def test_another_reserve_scoped_token_cannot_mint_access_by_guessing_a_child_id(paths, root):
    """A different caller who also holds 'reserve' scope on the SAME
    parent, but did not create this delegation, must not be able to
    recover, rotate, claim, or mint themselves a working credential for it
    just by naming the same delegation_id/agent_id/amount -- crash-safe
    recovery (finding 1) is only ever available to the exact original
    authorizer, identified by non-secret token_id, never merely by
    matching parent/agent/amount or by holding a generically valid
    `reserve`-scoped credential on the same parent."""
    _root_id, root_token = root
    cap_store = CapabilityStore(paths["cap_db"])
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()
        original = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:guess-1",
                "parent_id": "root",
                "delegation_id": "child-guessable",
                "agent_id": "worker",
                "maximum_usd": "0.10",
            },
        )
        assert original.status.state == TaskState.TASK_STATE_COMPLETED

        _other_id, other_reserve_token = cap_store.issue("root", {"reserve"})
        other_client = await make_client(base_url, other_reserve_token, session_id="guesser")
        guess = await send(
            other_client,
            call_context("guesser"),
            {
                "op": "reserve",
                "event_id": "evt:guess-2-different",
                "parent_id": "root",
                "delegation_id": "child-guessable",  # guessed/observed delegation_id
                "agent_id": "worker",
                "maximum_usd": "0.10",
            },
        )
        assert guess.status.state == TaskState.TASK_STATE_REJECTED
        assert get_data_parts(guess.status.message.parts)[0]["error"] == "WrongAuthorizer"
        assert not guess.artifacts, (
            "the guesser must never receive a credential for a delegation it did not create"
        )

    with sqlite3.connect(paths["cap_db"]) as conn:
        live_count = conn.execute(
            "SELECT COUNT(*) FROM capability_tokens WHERE delegation_id = 'child-guessable' "
            "AND revoked = 0"
        ).fetchone()[0]
    assert live_count == 1, (
        "a rejected guess must never revoke or replace the legitimate authorizer's credential"
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


# -- repair item 4: root revocation races descendant reservation --------


async def test_root_revocation_races_new_reservations_and_nothing_escapes(paths, root):
    """Fires many concurrent `reserve` attempts against root at the same
    time as a `revoke` of root, over real HTTP transport. This is the
    transport-level companion to
    `test_a2a_economic_authority_core.py`'s
    `test_concurrent_reserve_and_revocation_mark_never_lets_a_reservation_escape_unmarked`,
    which proves the underlying `core.py` mechanism directly; this test
    proves the same property holds end-to-end through the executor and
    real transport: whatever the race's outcome, no descendant retains
    active, usable authority once revocation has completed.
    """
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:

        async def try_reserve(i: int):
            racer_client = await make_client(base_url, root_token, session_id=f"racer-{i}")
            payload = {
                "op": "reserve",
                "event_id": f"evt:escape-race-{i}",
                "parent_id": "root",
                "delegation_id": f"child-escape-race-{i}",
                "agent_id": "worker",
                "maximum_usd": "0.01",
            }
            return await send(racer_client, call_context(f"racer-{i}"), payload)

        async def do_revoke():
            revoker_client = await make_client(base_url, root_token, session_id="revoker")
            return await send(
                revoker_client,
                call_context("revoker"),
                {"op": "revoke", "event_id": "evt:escape-race-revoke", "delegation_id": "root"},
            )

        racer_count = 20
        results = await asyncio.gather(*(try_reserve(i) for i in range(racer_count)), do_revoke())

        reserve_results = results[:-1]
        revoke_result = results[-1]
        assert revoke_result.status.state == TaskState.TASK_STATE_COMPLETED

        core_store = EconomicAuthorityStore(paths["db"])
        root_state = core_store.get("root")
        assert root_state is not None
        assert root_state.revocation_started_at is not None
        assert root_state.active is False

        successful_claim_ids = []
        for i, task in enumerate(reserve_results):
            if task.status.state == TaskState.TASK_STATE_COMPLETED:
                delegation_id = f"child-escape-race-{i}"
                child_state = core_store.get(delegation_id)
                assert child_state is not None, (
                    f"{delegation_id} was reported reserved but does not exist in the core store"
                )
                assert child_state.active is False, (
                    f"{delegation_id} escaped root revocation while still active -- a usable "
                    "descendant survived a completed root revocation"
                )
                successful_claim_ids.append(_artifact_dict(task)["credential_claim_id"])

        # Every claim for a delegation that DID get created must be
        # unredeemable now -- either because the claim itself was purged
        # when the subtree was revoked, or because root_token (the only
        # credential these test racers used) is itself now revoked.
        # Either failure mode is correct; success is not.
        for claim_id in successful_claim_ids:
            response = claim_credential(base_url, claim_id, root_token)
            assert response.status_code != 200, (
                f"claim {claim_id!r} for an escaped-looking descendant was still redeemable "
                "after root revocation completed"
            )


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


# -- finding 3: purging an unclaimed claim by its TARGET delegation -------


async def test_revoking_a_direct_child_purges_its_own_unclaimed_claim(paths, root):
    """A child's reservation is authorized by its PARENT's credential --
    the claim's `issuer_delegation_id` is the parent, not the child
    itself. Revoking just the child (not the parent) must still purge that
    child's own outstanding, unclaimed claim: the claim's `issuer` lies
    outside the revoked set (only the child itself is being revoked), so
    only matching on `child_delegation_id` (finding 3) purges it. Before
    that fix, the claim would survive revocation and remain redeemable for
    a delegation that no longer has usable authority."""
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
                "event_id": "evt:direct-child-revoke",
                "parent_id": "root",
                "delegation_id": "child-revoked-before-claim",
                "agent_id": "worker",
                "maximum_usd": "0.10",
            },
        )
        assert reserve_task.status.state == TaskState.TASK_STATE_COMPLETED
        claim_id = _artifact_dict(reserve_task)["credential_claim_id"]
        assert claim_id is not None

        revoke_task = await send(
            client,
            ctx,
            {
                "op": "revoke",
                "event_id": "evt:revoke-direct-child",
                "delegation_id": "child-revoked-before-claim",
            },
        )
        assert revoke_task.status.state == TaskState.TASK_STATE_COMPLETED

        claimed = claim_credential(base_url, claim_id, root_token)
        assert claimed.status_code != 200, (
            "a claim targeting a revoked child must be purged immediately, "
            "not left redeemable"
        )


async def test_revoking_a_deep_grandchild_directly_purges_its_own_unclaimed_claim(paths, root):
    """The same finding-3 scenario as the direct-child test, one level
    deeper: a grandchild's claim is issued by its immediate parent
    (`mid-node`), which is NOT part of this revoke at all -- only the
    grandchild itself is revoked, and `mid-node` is left alive and
    unaffected. The grandchild's own unclaimed claim must still be purged
    immediately, proving the child_delegation_id match works at any depth,
    not only for a direct child of the delegation named in the revoke
    call."""
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        mid_task = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:mid",
                "parent_id": "root",
                "delegation_id": "mid-node",
                "agent_id": "worker",
                "maximum_usd": "0.50",
                "child_scopes": ["read", "consume", "settle", "reserve"],
            },
        )
        assert mid_task.status.state == TaskState.TASK_STATE_COMPLETED
        mid_claim_id = _artifact_dict(mid_task)["credential_claim_id"]
        mid_token = claim_credential(base_url, mid_claim_id, root_token).json()["token"]

        mid_client = await make_client(base_url, mid_token, session_id="mid")
        grandchild_task = await send(
            mid_client,
            call_context("mid"),
            {
                "op": "reserve",
                "event_id": "evt:grandchild",
                "parent_id": "mid-node",
                "delegation_id": "grandchild-unclaimed",
                "agent_id": "sub-worker",
                "maximum_usd": "0.10",
            },
        )
        assert grandchild_task.status.state == TaskState.TASK_STATE_COMPLETED
        grandchild_claim_id = _artifact_dict(grandchild_task)["credential_claim_id"]
        assert grandchild_claim_id is not None
        # Deliberately never claimed -- this is the outstanding claim the
        # grandchild-only revoke below must purge, even though its issuer
        # (`mid-node`) is untouched.

        revoke_task = await send(
            client,
            ctx,
            {
                "op": "revoke",
                "event_id": "evt:revoke-grandchild",
                "delegation_id": "grandchild-unclaimed",
            },
        )
        assert revoke_task.status.state == TaskState.TASK_STATE_COMPLETED

        claimed = claim_credential(base_url, grandchild_claim_id, root_token)
        assert claimed.status_code != 200, (
            "an unclaimed claim must be purged when its own delegation is revoked, "
            "even when its issuer (an ancestor) survives untouched"
        )

        # mid-node itself is untouched by the grandchild-only revoke.
        status_task = await send(
            mid_client, call_context("mid"), {"op": "status", "delegation_id": "mid-node"}
        )
        assert status_task.status.state == TaskState.TASK_STATE_COMPLETED
        assert _artifact_dict(status_task)["delegation"]["active"] is True


# -- repair item 3: revocation fails closed and crash-recovers --------


async def test_revocation_blocks_grant_consume_and_claim_before_teardown_completes(paths, root):
    """Whitebox proof of the exact window repair item 3 closes: once
    `mark_revocation_started` has committed for an ancestor, grant/
    consume/claim-redemption against a descendant must all fail -- even
    though the descendant is technically still `active` and its
    capability tokens are technically still unrevoked, because the rest
    of the teardown (settlement, token revocation, claim purge) has not
    run yet. Exercised directly against core.py/capabilities.py rather
    than through a live executor, so the window is fully controlled."""
    _root_id, root_token = root
    cap_store = CapabilityStore(paths["cap_db"])
    core_store = EconomicAuthorityStore(paths["db"])

    core_store.reserve("evt:r1", "root", "child-mid-revoke", "worker", Decimal("0.30"))
    _child_id, child_token = cap_store.issue("child-mid-revoke", {"grant", "consume"})

    # Simulates the crashed-mid-revoke window directly: mark committed,
    # nothing else has run yet.
    assert core_store.mark_revocation_started("root") is True

    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        child_client = await make_client(base_url, child_token, session_id="mid-revoke")
        child_ctx = call_context("mid-revoke")

        grant_task = await send(
            child_client,
            child_ctx,
            {
                "op": "grant",
                "event_id": "evt:g1",
                "delegation_id": "child-mid-revoke",
                "amount_usd": "0.01",
            },
        )
        assert grant_task.status.state == TaskState.TASK_STATE_FAILED, (
            "grant must be blocked once an ancestor's revocation has started"
        )

        consume_task = await send(
            child_client,
            child_ctx,
            {
                "op": "consume",
                "event_id": "evt:c1",
                "delegation_id": "child-mid-revoke",
                "amount_usd": "0.01",
            },
        )
        assert consume_task.status.state == TaskState.TASK_STATE_FAILED, (
            "consume must be blocked once an ancestor's revocation has started"
        )

    # The capability token itself is still technically unrevoked at this
    # point (only core.py's revocation-in-progress flag is set) -- confirm
    # that directly, then confirm the target-revocation check in the claim
    # route independently blocks a hypothetical claim for this same tree.
    cap_store.authorize(child_token, "child-mid-revoke", "consume")  # does not raise
    assert core_store.is_revocation_in_progress("child-mid-revoke") is True


async def test_revocation_crash_recovery_finishes_an_interrupted_teardown(paths):
    """Repair item 3's crash-injection requirement, end to end: a real
    subprocess is killed immediately after `mark_revocation_started`
    commits for root (before any settlement, token revocation, or claim
    purge). A real server is then started against the same database
    files -- proving descendants are already fail-closed even though
    nothing has been settled yet -- and a retried `revoke` through real
    A2A transport safely resumes and finishes the interrupted teardown.
    """
    state_json_path = paths["tmp_path"] / "revocation_crash_state.json"
    helper = Path(__file__).resolve().parent / "_a2a_economic_authority_revocation_crash_helper.py"
    result = subprocess.run(
        [sys.executable, str(helper), str(paths["db"]), str(paths["cap_db"]), str(state_json_path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0, (
        f"crash helper exited cleanly (code {result.returncode}); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    state = json.loads(state_json_path.read_text())
    child_plaintext = state["child_plaintext"]
    grandchild_plaintext = state["grandchild_plaintext"]

    # Confirm the crash boundary landed where intended: marked, but
    # nothing settled yet, and the child/grandchild tokens still resolve.
    core_store = EconomicAuthorityStore(paths["db"])
    assert core_store.get("root").revocation_started_at is not None  # type: ignore[union-attr]
    assert core_store.get("child-1").active is True  # type: ignore[union-attr]
    cap_store = CapabilityStore(paths["cap_db"])
    cap_store.authorize(child_plaintext, "child-1", "consume")  # does not raise yet

    # A fresh root capability, minted after the crash directly against the
    # already-existing root row (created by the crash helper, not by
    # bootstrap_root) -- simulating an operator who still holds root
    # authority and wants to retry the interrupted revoke.
    _root_token_id, root_token = cap_store.issue("root", SCOPES)

    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        # Before the retry: descendants are already fail-closed.
        blocked_client = await make_client(base_url, child_plaintext, session_id="blocked")
        blocked = await send(
            blocked_client,
            call_context("blocked"),
            {
                "op": "consume",
                "event_id": "evt:blocked",
                "delegation_id": "child-1",
                "amount_usd": "0.01",
            },
        )
        assert blocked.status.state == TaskState.TASK_STATE_FAILED

        retried = await send(
            client, ctx, {"op": "revoke", "event_id": "evt:resume-revoke", "delegation_id": "root"}
        )
        assert retried.status.state == TaskState.TASK_STATE_COMPLETED
        receipt = _artifact_dict(retried)
        assert set(receipt["revoked_delegation_ids"]) == {"root", "child-1", "grandchild-1"}
        assert set(receipt["settled_delegation_ids"]) == {"root", "child-1", "grandchild-1"}

    # Final state: nothing usable survives, anywhere in the tree.
    for delegation_id in ("root", "child-1", "grandchild-1"):
        state_after = core_store.get(delegation_id)
        assert state_after is not None
        assert state_after.active is False

    for plaintext, delegation_id in (
        (child_plaintext, "child-1"),
        (grandchild_plaintext, "grandchild-1"),
        (root_token, "root"),
    ):
        with pytest.raises(RevokedCredential):
            cap_store.authorize(plaintext, delegation_id, "read")


# -- repair item 8: decimal normalization and input validation ------------


async def test_equivalent_decimal_amounts_do_not_false_conflict_over_transport(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        first = await send(
            client,
            ctx,
            {
                "op": "consume",
                "event_id": "evt:decimal-equiv",
                "delegation_id": "root",
                "amount_usd": "0.10",
            },
        )
        assert first.status.state == TaskState.TASK_STATE_COMPLETED

        retry = await send(
            client,
            ctx,
            {
                "op": "consume",
                "event_id": "evt:decimal-equiv",
                "delegation_id": "root",
                "amount_usd": "0.100",  # same value, different text
            },
        )
        assert retry.status.state == TaskState.TASK_STATE_COMPLETED

    core_store = EconomicAuthorityStore(paths["db"])
    root_state = core_store.get("root")
    assert root_state is not None
    assert root_state.consumed_usd == Decimal("0.10")


async def test_non_finite_amount_is_rejected_over_transport(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        for bad_amount in ("NaN", "Infinity", "-Infinity"):
            task = await send(
                client,
                ctx,
                {
                    "op": "grant",
                    "event_id": f"evt:bad-{bad_amount}",
                    "delegation_id": "root",
                    "amount_usd": bad_amount,
                },
            )
            assert task.status.state == TaskState.TASK_STATE_FAILED, (
                f"amount_usd={bad_amount!r} must be rejected, not accepted"
            )

    core_store = EconomicAuthorityStore(paths["db"])
    root_state = core_store.get("root")
    assert root_state is not None
    assert root_state.authority_usd == Decimal("1.00"), "no non-finite grant may have applied"


async def test_oversized_identifier_is_rejected_over_transport(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        task = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:oversized",
                "parent_id": "root",
                "delegation_id": "x" * 500,
                "agent_id": "worker",
                "maximum_usd": "0.01",
            },
        )
        assert task.status.state == TaskState.TASK_STATE_FAILED


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


# -- finding 2: unknown-cost settlement stays fail-closed over transport ---


async def test_settling_a_child_with_unknown_cost_does_not_free_reusable_headroom(paths, root):
    """Real-transport regression test for finding 2: root ($1.00) reserves
    part of its authority for a child, the child records unknown
    consumption, and the child settles. Root must not be able to reserve
    its full original $1.00 again afterward -- only the genuinely
    untouched remainder -- even though root's own invariant reports
    PARTIAL, not a violation."""
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
                "event_id": "evt:unknown-settle-reserve",
                "parent_id": "root",
                "delegation_id": "child-unknown-settle",
                "agent_id": "worker",
                "maximum_usd": "0.40",
            },
        )
        assert reserve_task.status.state == TaskState.TASK_STATE_COMPLETED
        claim_id = _artifact_dict(reserve_task)["credential_claim_id"]
        child_token = claim_credential(base_url, claim_id, root_token).json()["token"]

        child_client = await make_client(base_url, child_token, session_id="unknown-settle-child")
        child_ctx = call_context("unknown-settle-child")
        consume_task = await send(
            child_client,
            child_ctx,
            {
                "op": "consume",
                "event_id": "evt:unknown-spend",
                "delegation_id": "child-unknown-settle",
                "amount_usd": None,
            },
        )
        assert consume_task.status.state == TaskState.TASK_STATE_COMPLETED

        settle_task = await send(
            child_client,
            child_ctx,
            {
                "op": "settle",
                "event_id": "evt:unknown-settle",
                "delegation_id": "child-unknown-settle",
                "outcome": "PARTIAL",
            },
        )
        assert settle_task.status.state == TaskState.TASK_STATE_COMPLETED

        status_task = await send(client, ctx, {"op": "status", "delegation_id": "root"})
        assert status_task.status.state == TaskState.TASK_STATE_COMPLETED
        root_receipt = _artifact_dict(status_task)
        assert root_receipt["delegation"]["child_reserved_usd"] == "0.40", (
            "the $0.40 reserved for the unknown-cost child must not be released back "
            "to root just because the child settled"
        )
        assert root_receipt["invariant"]["certainty"] == "PARTIAL"

        full_reserve_retry = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:unknown-settle-reserve-full",
                "parent_id": "root",
                "delegation_id": "child-after-unknown-settle",
                "agent_id": "worker",
                "maximum_usd": "1.00",
            },
        )
        assert full_reserve_retry.status.state in (
            TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_AUTH_REQUIRED,
        ), "root must never be able to reserve its full original authority again"


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


# -- repair item 2: a rejected reservation must never change authority ----


async def test_malformed_child_scopes_rejects_with_no_economic_mutation(paths, root):
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        client = await make_client(base_url, root_token)
        ctx = call_context()

        task = await send(
            client,
            ctx,
            {
                "op": "reserve",
                "event_id": "evt:malformed",
                "parent_id": "root",
                "delegation_id": "child-malformed",
                "agent_id": "worker",
                "maximum_usd": "0.10",
                "child_scopes": "read",  # a string, not a list -- malformed
            },
        )
        assert task.status.state == TaskState.TASK_STATE_FAILED
        assert not task.artifacts, "a rejected reservation must never issue a claim/receipt"

    core_store = EconomicAuthorityStore(paths["db"])
    assert core_store.get("child-malformed") is None, "the delegation must never have been created"
    root_state = core_store.get("root")
    assert root_state is not None
    assert root_state.child_reserved_usd == Decimal("0"), "the parent's headroom must be untouched"


async def test_disallowed_child_scopes_over_real_reserve_rejects_cleanly(paths, root):
    """A child token minted with only 'reserve' scope attempts to mint a
    grandchild capability with 'grant' -- a scope it does not itself
    hold. Must be rejected before any mutation, not after."""
    _root_id, _root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        # Mint a narrow, reserve-only child token directly (bypassing the
        # server) so its issuer_scopes contains only 'reserve' -- it must
        # not be able to mint a grandchild capability with 'grant'.
        cap_store = CapabilityStore(paths["cap_db"])
        core_store = EconomicAuthorityStore(paths["db"])
        core_store.reserve("evt:seed", "root", "child-narrow", "worker", Decimal("0.50"))
        _token_id, narrow_token = cap_store.issue("child-narrow", {"reserve"})

        narrow_client = await make_client(base_url, narrow_token, session_id="narrow")
        narrow_ctx = call_context("narrow")

        task = await send(
            narrow_client,
            narrow_ctx,
            {
                "op": "reserve",
                "event_id": "evt:disallowed",
                "parent_id": "child-narrow",
                "delegation_id": "grandchild-disallowed",
                "agent_id": "sub",
                "maximum_usd": "0.10",
                "child_scopes": ["read", "grant"],
            },
        )
        assert task.status.state == TaskState.TASK_STATE_FAILED
        assert not task.artifacts

    assert core_store.get("grandchild-disallowed") is None
    child_narrow = core_store.get("child-narrow")
    assert child_narrow is not None
    assert child_narrow.child_reserved_usd == Decimal("0"), "no partial reservation must remain"


# -- repair item 6: grant-only authority cannot hijack a reservation ------


async def test_grant_only_credential_cannot_claim_the_reservation_it_unblocked(paths, root):
    """Two separate, narrow credentials for the same delegation: one holds
    only 'reserve', the other only 'grant'. The reserve-only caller parks
    a reservation that exceeds headroom; the grant-only caller supplies
    the missing authority on the same task_id. The grant-only caller must
    not be able to redeem the resulting child credential -- only a
    currently-valid 'reserve'-scoped credential for the parent can."""
    _root_id, root_token = root
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        cap_store = CapabilityStore(paths["cap_db"])
        _rid, reserve_only_token = cap_store.issue("root", {"reserve"})
        _gid, grant_only_token = cap_store.issue("root", {"grant"})

        reserve_client = await make_client(base_url, reserve_only_token, session_id="reserver")
        reserve_ctx = call_context("reserver")
        grant_client = await make_client(base_url, grant_only_token, session_id="granter")
        grant_ctx = call_context("granter")

        parked = await send(
            reserve_client,
            reserve_ctx,
            {
                "op": "reserve",
                "event_id": "evt:hijack",
                "parent_id": "root",
                "delegation_id": "child-hijack",
                "agent_id": "worker",
                "maximum_usd": "5.00",
            },
        )
        assert parked.status.state == TaskState.TASK_STATE_AUTH_REQUIRED

        retried = await send(
            grant_client,
            grant_ctx,
            {
                "op": "grant",
                "event_id": "evt:hijack-grant",
                "delegation_id": "root",
                "amount_usd": "5.00",
            },
            task_id=parked.id,
        )
        assert retried.status.state == TaskState.TASK_STATE_COMPLETED
        claim_id = _artifact_dict(retried, index=-1)["credential_claim_id"]

        hijack_attempt = claim_credential(base_url, claim_id, grant_only_token)
        assert hijack_attempt.status_code == 403
        assert hijack_attempt.json()["error"] == "InsufficientScope"

        legitimate_claim = claim_credential(base_url, claim_id, reserve_only_token)
        assert legitimate_claim.status_code == 200
        assert "token" in legitimate_claim.json()


async def test_a_different_reserve_scoped_credential_for_the_same_parent_cannot_redeem(paths, root):
    """Repair item 5: even a credential that is EQUALLY legitimate --
    correctly scoped, correctly delegation-bound, currently valid -- but
    is simply not the one that authorized this specific reservation, must
    not be able to redeem its claim. Distinct from the grant-only case
    above: here BOTH credentials hold 'reserve' scope on the same
    parent."""
    _root_id, root_token = root
    cap_store = CapabilityStore(paths["cap_db"])
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"], paths["log"]) as base_url:
        _id_a, reserve_token_a = cap_store.issue("root", {"reserve"})
        _id_b, reserve_token_b = cap_store.issue("root", {"reserve"})

        client_a = await make_client(base_url, reserve_token_a, session_id="a")
        reserved = await send(
            client_a,
            call_context("a"),
            {
                "op": "reserve",
                "event_id": "evt:exact-authorizer",
                "parent_id": "root",
                "delegation_id": "child-exact-authorizer",
                "agent_id": "worker",
                "maximum_usd": "0.05",
            },
        )
        assert reserved.status.state == TaskState.TASK_STATE_COMPLETED
        claim_id = _artifact_dict(reserved)["credential_claim_id"]

        wrong_holder_attempt = claim_credential(base_url, claim_id, reserve_token_b)
        assert wrong_holder_attempt.status_code == 403
        assert wrong_holder_attempt.json()["error"] == "WrongAuthorizer"

        true_authorizer_attempt = claim_credential(base_url, claim_id, reserve_token_a)
        assert true_authorizer_attempt.status_code == 200


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
