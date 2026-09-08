"""Inferrail Economic Authority — A2A AgentExecutor (Phase B).

Dispatches six direct, caller-initiated operations over real A2A transport:
reserve, grant, consume, settle, status (read), revoke. Every operation is
synchronous within one `execute()` call except `reserve`, which may park at
`TASK_STATE_AUTH_REQUIRED` when the parent delegation's headroom is
insufficient -- exactly the validated "insufficient authority -> grant ->
retry" pattern, reproduced here as a direct grant/retry rather than any
automatic call to another agent.

Nothing in this module ever calls another agent or performs any outbound
network request of its own -- see
`test_no_outbound_calls_in_executor_and_server_modules` for the enforced
check.

Authorization: every mutating and read operation requires a bearer
capability token, extracted from the HTTP `Authorization` header via
`RequestContext.call_context.state['headers']` (populated by the A2A SDK's
`DefaultServerCallContextBuilder` from the real HTTP request -- never from
A2A message content). See `capabilities.py` for the token model and
`server.py` for the documented SDK limitation this design works around when
handing a newly-minted child capability back to a caller.
"""

from __future__ import annotations

import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

_HOSTED_DIR = Path(__file__).resolve().parent
if str(_HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(_HOSTED_DIR))

from a2a.helpers import get_data_parts, new_data_part, new_task_from_user_message  # noqa: E402
from a2a.server.agent_execution import AgentExecutor, RequestContext  # noqa: E402
from a2a.server.events import EventQueue  # noqa: E402
from a2a.server.tasks import TaskUpdater  # noqa: E402
from a2a.types import Task, TaskState  # noqa: E402
from capabilities import (  # noqa: E402
    CapabilityError,
    CapabilityStore,
    InMemoryCredentialHandoff,
)
from core import DelegationState, EconomicAuthorityStore, InvariantResult  # noqa: E402

DEFAULT_CHILD_SCOPES = frozenset({"read", "consume", "settle"})

_RESERVE_REQUIRED_FIELDS = frozenset(
    {"event_id", "parent_id", "delegation_id", "agent_id", "maximum_usd"}
)


def _state_to_dict(state: DelegationState) -> dict[str, Any]:
    return {
        "delegation_id": state.delegation_id,
        "parent_delegation_id": state.parent_delegation_id,
        "agent_id": state.agent_id,
        "authority_usd": str(state.authority_usd),
        "consumed_usd": str(state.consumed_usd),
        "child_reserved_usd": str(state.child_reserved_usd),
        "released_usd": str(state.released_usd),
        "active_reservation_usd": str(state.active_reservation_usd),
        "unknown_cost_count": state.unknown_cost_count,
        "active": state.active,
        "outcome": state.outcome,
    }


def _invariant_to_dict(result: InvariantResult) -> dict[str, Any]:
    return {
        "satisfied_on_known_values": result.satisfied_on_known_values,
        "certainty": result.certainty,
        "label": result.label,
        "detail": result.detail,
    }


class EconomicAuthorityExecutor(AgentExecutor):
    """Direct-operation-only executor over `EconomicAuthorityStore`."""

    def __init__(
        self,
        core: EconomicAuthorityStore,
        capabilities: CapabilityStore,
        handoff: InMemoryCredentialHandoff,
    ) -> None:
        self.core = core
        self.capabilities = capabilities
        self.handoff = handoff

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(
            event_queue, cast(str, context.task_id), cast(str, context.context_id)
        )
        if context.current_task is None and context.message is not None:
            # The framework requires the very first event of a new task to be
            # an actual Task object, not a bare status/artifact update -- see
            # `a2a.server.agent_execution.active_task._handle_task_modification_event`.
            await event_queue.enqueue_event(new_task_from_user_message(context.message))
        parts = get_data_parts(context.message.parts) if context.message else []
        if not parts or not isinstance(parts[0], dict) or "op" not in parts[0]:
            await self._fail(
                updater, "message must contain exactly one JSON data part with an 'op' field"
            )
            return

        payload = parts[0]
        op = payload["op"]
        token = self._bearer_token(context)
        task = context.current_task

        try:
            if task is not None and task.status.state == TaskState.TASK_STATE_AUTH_REQUIRED:
                if op != "grant":
                    await self._fail(
                        updater, "task is parked awaiting a grant; retry with op='grant'"
                    )
                    return
                await self._continue_after_grant(payload, token, updater, task)
                return

            if op == "reserve":
                await self._do_reserve(payload, token, updater)
            elif op == "grant":
                await self._do_grant(payload, token, updater)
            elif op == "consume":
                await self._do_consume(payload, token, updater)
            elif op == "settle":
                await self._do_settle(payload, token, updater)
            elif op == "status":
                await self._do_status(payload, token, updater)
            elif op == "revoke":
                await self._do_revoke(payload, token, updater)
            else:
                await self._fail(updater, f"unknown op {op!r}")
        except CapabilityError as exc:
            await updater.reject(
                message=updater.new_agent_message(
                    [new_data_part({"error": type(exc).__name__, "detail": str(exc)})]
                )
            )
        except (KeyError, InvalidOperation, ValueError) as exc:
            await self._fail(updater, f"invalid request: {exc}")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(
            event_queue, cast(str, context.task_id), cast(str, context.context_id)
        )
        await updater.cancel()

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _bearer_token(context: RequestContext) -> str | None:
        headers = context.call_context.state.get("headers", {}) if context.call_context else {}
        auth_header = str(headers.get("authorization") or "")
        if not auth_header:
            return None
        scheme, _, value = auth_header.partition(" ")
        token = value.strip()
        if scheme.lower() != "bearer" or not token:
            return None
        return token

    @staticmethod
    async def _fail(updater: TaskUpdater, message: str) -> None:
        await updater.failed(message=updater.new_agent_message([new_data_part({"error": message})]))

    async def _complete_with_status(
        self, updater: TaskUpdater, delegation_id: str, note: str
    ) -> None:
        state = self.core.get(delegation_id)
        lineage = self.core.lineage(delegation_id)
        invariant = self.core.invariant(delegation_id)
        receipt = {
            "note": note,
            "delegation": _state_to_dict(state) if state else None,
            "lineage": [_state_to_dict(s) for s in lineage],
            "invariant": _invariant_to_dict(invariant),
        }
        await updater.add_artifact([new_data_part(receipt)], name="receipt")
        await updater.complete()

    def _subtree_ids(self, delegation_id: str) -> list[str]:
        """Delegation_id plus every descendant, discovered breadth-first."""
        ids = [delegation_id]
        frontier = [delegation_id]
        while frontier:
            current = frontier.pop()
            for child in self.core.children(current):
                ids.append(child.delegation_id)
                frontier.append(child.delegation_id)
        return ids

    def _find_pending_operation(self, task: Task) -> dict[str, Any] | None:
        for artifact in task.artifacts:
            for item in get_data_parts(artifact.parts):
                if isinstance(item, dict) and "pending_operation" in item:
                    pending = item["pending_operation"]
                    return pending if isinstance(pending, dict) else None
        return None

    # -- reserve, with the AUTH_REQUIRED park/retry path -------------------

    async def _do_reserve(
        self, payload: dict[str, Any], token: str | None, updater: TaskUpdater
    ) -> None:
        missing = _RESERVE_REQUIRED_FIELDS - payload.keys()
        if missing:
            await self._fail(updater, f"reserve requires {sorted(_RESERVE_REQUIRED_FIELDS)}")
            return
        parent_id = payload["parent_id"]
        self.capabilities.authorize(token, parent_id, "reserve")
        issuer_scopes = self.capabilities.live_scopes(token) or frozenset()
        pending = dict(payload)
        pending["issuer_scopes"] = sorted(issuer_scopes)
        await self._finish_reserve(pending, issuer_scopes, token, updater, retry=False)

    async def _continue_after_grant(
        self, payload: dict[str, Any], token: str | None, updater: TaskUpdater, task: Task
    ) -> None:
        pending = self._find_pending_operation(task)
        if pending is None:
            await self._fail(updater, "no pending operation to resume on this task")
            return
        parent_id = payload.get("delegation_id")
        event_id = payload.get("event_id")
        if not parent_id or not event_id or "amount_usd" not in payload:
            await self._fail(updater, "grant requires delegation_id, event_id, amount_usd")
            return
        if parent_id != pending.get("parent_id"):
            await self._fail(updater, "grant must target the parked reservation's parent_id")
            return

        self.capabilities.authorize(token, parent_id, "grant")
        amount_usd = Decimal(str(payload["amount_usd"]))
        ok = self.core.grant(event_id, parent_id, amount_usd)
        if not ok:
            await self._fail(updater, "grant rejected")
            return

        issuer_scopes = frozenset(pending.get("issuer_scopes", []))
        await self._finish_reserve(pending, issuer_scopes, token, updater, retry=True)

    async def _finish_reserve(
        self,
        pending: dict[str, Any],
        issuer_scopes: frozenset[str],
        completing_token: str | None,
        updater: TaskUpdater,
        retry: bool,
    ) -> None:
        parent_id = pending["parent_id"]
        delegation_id = pending["delegation_id"]
        agent_id = pending["agent_id"]
        event_id = pending["event_id"]
        maximum_usd = Decimal(str(pending["maximum_usd"]))

        parent = self.core.get(parent_id)
        if parent is None or not parent.active or parent.unknown_cost_count:
            await self._fail(updater, "parent delegation is not available to reserve against")
            return

        shortfall = maximum_usd - parent.active_reservation_usd
        if shortfall > 0:
            if retry:
                await self._fail(
                    updater, "granted amount is still insufficient for the pending reservation"
                )
                return
            await updater.add_artifact(
                [new_data_part({"pending_operation": pending, "shortfall_usd": str(shortfall)})],
                name="pending-operation",
            )
            await updater.requires_auth(
                message=updater.new_agent_message(
                    [
                        new_data_part(
                            {
                                "status": "insufficient_parent_headroom",
                                "parent_id": parent_id,
                                "shortfall_usd": str(shortfall),
                                "hint": (
                                    "send a 'grant' op for at least shortfall_usd of additional "
                                    "authority to parent_id, on this same task_id, to retry"
                                ),
                            }
                        )
                    ]
                )
            )
            return

        ok = self.core.reserve(event_id, parent_id, delegation_id, agent_id, maximum_usd)
        if not ok:
            await self._fail(updater, "reserve rejected")
            return

        requested_scopes = pending.get("child_scopes")
        if requested_scopes is None:
            child_scopes = DEFAULT_CHILD_SCOPES & issuer_scopes
            if not child_scopes:
                child_scopes = issuer_scopes or frozenset({"read"})
        else:
            requested = frozenset(requested_scopes)
            disallowed = requested - issuer_scopes
            if disallowed:
                await self._fail(
                    updater, f"cannot grant scopes the caller does not hold: {sorted(disallowed)}"
                )
                return
            child_scopes = requested

        token_id, plaintext = self.capabilities.issue(delegation_id, child_scopes)
        claim_id = self.handoff.create(token_id, plaintext, completing_token or "")
        await updater.add_artifact(
            [
                new_data_part(
                    {
                        "delegation_id": delegation_id,
                        "parent_delegation_id": parent_id,
                        "agent_id": agent_id,
                        "credential_claim_id": claim_id,
                        "credential_claim_scopes": sorted(child_scopes),
                        "credential_claim_instructions": (
                            "POST /capabilities/claim on this server with your OWN "
                            "bearer token (the one that authorized this reservation) "
                            "and this claim_id, once and promptly, to receive the "
                            "child's capability token. The token itself never appears "
                            "in this A2A task."
                        ),
                    }
                )
            ],
            name="receipt",
        )
        await updater.complete()

    # -- grant / consume / settle / status ---------------------------------

    async def _do_grant(
        self, payload: dict[str, Any], token: str | None, updater: TaskUpdater
    ) -> None:
        delegation_id = payload.get("delegation_id")
        event_id = payload.get("event_id")
        if not delegation_id or not event_id or "amount_usd" not in payload:
            await self._fail(updater, "grant requires delegation_id, event_id, amount_usd")
            return
        self.capabilities.authorize(token, delegation_id, "grant")
        amount_usd = Decimal(str(payload["amount_usd"]))
        ok = self.core.grant(event_id, delegation_id, amount_usd)
        if not ok:
            await self._fail(
                updater, "grant rejected (unknown delegation, inactive, or negative amount)"
            )
            return
        await self._complete_with_status(updater, delegation_id, note="granted")

    async def _do_consume(
        self, payload: dict[str, Any], token: str | None, updater: TaskUpdater
    ) -> None:
        delegation_id = payload.get("delegation_id")
        event_id = payload.get("event_id")
        if not delegation_id or not event_id or "amount_usd" not in payload:
            await self._fail(
                updater,
                "consume requires delegation_id, event_id, amount_usd (amount_usd may be null)",
            )
            return
        self.capabilities.authorize(token, delegation_id, "consume")
        raw_amount = payload["amount_usd"]
        amount_usd = None if raw_amount is None else Decimal(str(raw_amount))
        ok = self.core.consume(event_id, delegation_id, amount_usd)
        if not ok:
            await self._fail(
                updater,
                "consume rejected (unknown delegation, inactive, or exceeds available authority)",
            )
            return
        await self._complete_with_status(updater, delegation_id, note="consumed")

    async def _do_settle(
        self, payload: dict[str, Any], token: str | None, updater: TaskUpdater
    ) -> None:
        delegation_id = payload.get("delegation_id")
        event_id = payload.get("event_id")
        outcome = payload.get("outcome")
        if not delegation_id or not event_id or not outcome:
            await self._fail(updater, "settle requires delegation_id, event_id, outcome")
            return
        self.capabilities.authorize(token, delegation_id, "settle")
        ok = self.core.settle(event_id, delegation_id, outcome)
        if not ok:
            await self._fail(updater, "settle rejected (unknown delegation or already inactive)")
            return
        await self._complete_with_status(updater, delegation_id, note="settled")

    async def _do_status(
        self, payload: dict[str, Any], token: str | None, updater: TaskUpdater
    ) -> None:
        delegation_id = payload.get("delegation_id")
        if not delegation_id:
            await self._fail(updater, "status requires delegation_id")
            return
        self.capabilities.authorize(token, delegation_id, "read")
        await self._complete_with_status(updater, delegation_id, note="status")

    # -- revoke: settle the whole subtree bottom-up, then revoke tokens ----

    def _authorize_revoke(self, token: str | None, delegation_id: str) -> None:
        """A revoke-scoped token authorizes revoking its own delegation_id
        OR any descendant of it -- this is what lets a root owner revoke its
        complete descendant tree (required), not just a delegation it holds
        an exact token for. A token scoped to an unrelated delegation (not
        an ancestor) never authorizes this, so this still cannot be used to
        control a sibling or unrelated delegation."""
        lineage = self.core.lineage(delegation_id)
        candidates = [state.delegation_id for state in lineage] if lineage else [delegation_id]
        last_error: CapabilityError | None = None
        for candidate in reversed(candidates):  # delegation_id itself, then parent, ..., up to root
            try:
                self.capabilities.authorize(token, candidate, "revoke")
                return
            except CapabilityError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def _do_revoke(
        self, payload: dict[str, Any], token: str | None, updater: TaskUpdater
    ) -> None:
        delegation_id = payload.get("delegation_id")
        event_id = payload.get("event_id")
        if not delegation_id or not event_id:
            await self._fail(updater, "revoke requires delegation_id, event_id")
            return
        outcome = payload.get("outcome") or "REVOKED"
        self._authorize_revoke(token, delegation_id)

        subtree = self._subtree_ids(delegation_id)
        settled: list[str] = []
        for dep_id in reversed(subtree):  # leaves first, target last
            state = self.core.get(dep_id)
            if state is not None and state.active:
                self.core.settle(f"{event_id}:{dep_id}", dep_id, outcome)
                settled.append(dep_id)
        revoked_token_count = self.capabilities.revoke_for_delegations(subtree)

        await updater.add_artifact(
            [
                new_data_part(
                    {
                        "revoked_delegation_ids": subtree,
                        "settled_delegation_ids": settled,
                        "revoked_token_count": revoked_token_count,
                    }
                )
            ],
            name="receipt",
        )
        await updater.complete()
