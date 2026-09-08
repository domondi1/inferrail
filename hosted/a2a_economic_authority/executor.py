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
    SCOPES,
    CapabilityError,
    CapabilityStore,
    InMemoryCredentialHandoff,
)
from core import (  # noqa: E402
    DelegationState,
    EconomicAuthorityStore,
    EventConflict,
    InvariantResult,
)

DEFAULT_CHILD_SCOPES = frozenset({"read", "consume", "settle"})

_RESERVE_REQUIRED_FIELDS = frozenset(
    {"event_id", "parent_id", "delegation_id", "agent_id", "maximum_usd"}
)

# Reserved namespace for event_ids this executor generates internally
# (currently: the per-descendant settlement events a tree revocation
# issues). No caller-supplied event_id may use this prefix -- otherwise a
# caller could pick an event_id that collides with (and, per core.py's
# EventConflict semantics, obstructs) an internal revocation-settlement
# event for the same delegation. See `_do_revoke` and repair item 3.
RESERVED_EVENT_ID_PREFIX = "__a2a_economic_authority_internal__:"


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

        raw_event_id = payload.get("event_id")
        if isinstance(raw_event_id, str) and raw_event_id.startswith(RESERVED_EVENT_ID_PREFIX):
            await self._fail(
                updater, "event_id may not use the reserved internal prefix"
            )
            return

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
        except EventConflict as exc:
            await self._fail(updater, f"event_id conflict: {exc}")
        except (KeyError, InvalidOperation, ValueError) as exc:
            await self._fail(updater, f"invalid request: {exc}")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancels the current task -- but only for a caller who can prove
        they hold at least `read` authority over the delegation this task
        belongs to. `on_cancel_task` is disabled entirely at the transport
        layer (see `access_control.py`), so this method is unreachable via
        HTTP today; it is hardened anyway as defense-in-depth, since an
        `AgentExecutor.cancel()` that unconditionally cancels is exactly
        the kind of unauthenticated-mutation bug that must not exist even
        when nothing currently calls it.
        """
        task = context.current_task
        delegation_id = self._delegation_id_of_task(task) if task is not None else None
        if delegation_id is None:
            return  # no identifiable owner to check against; fail closed, do nothing
        token = self._bearer_token(context)
        try:
            self.capabilities.authorize(token, delegation_id, "read")
        except CapabilityError:
            return  # unauthenticated/unauthorized cancellation is refused, not performed
        updater = TaskUpdater(
            event_queue, cast(str, context.task_id), cast(str, context.context_id)
        )
        await updater.cancel()

    @staticmethod
    def _delegation_id_of_task(task: Task) -> str | None:
        """Recovers the delegation this task is about from its own initial
        request message -- every op payload this executor accepts carries
        either `delegation_id` or (for `reserve`) `parent_id`."""
        if not task.history:
            return None
        for item in get_data_parts(task.history[0].parts):
            if isinstance(item, dict):
                delegation_id = item.get("delegation_id") or item.get("parent_id")
                if isinstance(delegation_id, str):
                    return delegation_id
        return None

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
        info = self.capabilities.authorize(token, parent_id, "reserve")
        issuer_scopes = info.scopes
        pending = dict(payload)
        pending["issuer_scopes"] = sorted(issuer_scopes)
        # Repair item 5: the non-secret token_id of the credential that
        # authorized THIS reservation is preserved across the park/grant/
        # retry cycle -- the resulting claim is bound to exactly this
        # credential, never to "whichever credential currently holds
        # reserve scope on the parent". Never the plaintext token itself.
        pending["reserve_authorizer_token_id"] = info.token_id
        await self._finish_reserve(pending, issuer_scopes, updater, retry=False)

    async def _continue_after_grant(
        self, payload: dict[str, Any], token: str | None, updater: TaskUpdater, task: Task
    ) -> None:
        """Applies a grant to the parked reservation's parent and, if that
        clears the shortfall, completes the reservation.

        Repair item 6 (grant/reserve separation): this method authorizes
        only the *grant* itself (`grant` scope on `parent_id`). It never
        re-authorizes `reserve` -- the original reservation was already
        authorized before it parked. And critically, the resulting child
        credential's claim (minted in `_finish_reserve`) is always bound
        to the EXACT credential that authorized the original reserve call
        (`pending["reserve_authorizer_token_id"]`, carried through
        unchanged from `_do_reserve` -- never overwritten with the grant
        caller's own token_id) -- so a grant-only credential can unblock a
        parked reservation but can never itself redeem, resume, or hijack
        it (repair item 5).
        """
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
        await self._finish_reserve(pending, issuer_scopes, updater, retry=True)

    def _validate_child_scopes(
        self, requested_scopes: Any, issuer_scopes: frozenset[str]
    ) -> tuple[bool, frozenset[str] | str]:
        """Validates and normalizes `child_scopes` -- returns `(True,
        scopes)` on success or `(False, error_message)` on failure.

        Repair item 2: this must run, and any failure must be handled,
        BEFORE `core.reserve()` is ever called. A malformed or disallowed
        `child_scopes` value must never leave a delegation, a parent's
        drawn-down headroom, an economic event, or an issued capability
        behind with no way for the caller to use it.
        """
        if requested_scopes is None:
            child_scopes = DEFAULT_CHILD_SCOPES & issuer_scopes
            if not child_scopes:
                child_scopes = issuer_scopes or frozenset({"read"})
            return True, child_scopes
        if not isinstance(requested_scopes, list) or not all(
            isinstance(item, str) for item in requested_scopes
        ):
            return False, "child_scopes must be a list of scope strings"
        requested = frozenset(requested_scopes)
        if not requested:
            return False, "child_scopes must not be empty"
        unknown = requested - SCOPES
        if unknown:
            return False, f"child_scopes contains unknown scopes: {sorted(unknown)}"
        disallowed = requested - issuer_scopes
        if disallowed:
            return False, f"cannot grant scopes the caller does not hold: {sorted(disallowed)}"
        return True, requested

    async def _finish_reserve(
        self,
        pending: dict[str, Any],
        issuer_scopes: frozenset[str],
        updater: TaskUpdater,
        retry: bool,
    ) -> None:
        parent_id = pending["parent_id"]
        delegation_id = pending["delegation_id"]
        agent_id = pending["agent_id"]
        event_id = pending["event_id"]
        maximum_usd = Decimal(str(pending["maximum_usd"]))
        authorizer_token_id = pending["reserve_authorizer_token_id"]

        parent = self.core.get(parent_id)
        if parent is None or not parent.active or parent.unknown_cost_count:
            await self._fail(updater, "parent delegation is not available to reserve against")
            return

        # Validate every fallible input BEFORE any economic mutation --
        # see `_validate_child_scopes`'s docstring.
        ok, child_scopes_or_error = self._validate_child_scopes(
            pending.get("child_scopes"), issuer_scopes
        )
        if not ok:
            await self._fail(updater, str(child_scopes_or_error))
            return
        assert isinstance(child_scopes_or_error, frozenset)
        child_scopes = child_scopes_or_error

        # A delegation_id that already exists is a retry (or a conflicting
        # reuse) of an existing reservation, not a request for new parent
        # headroom -- its authority was already carved out of the parent
        # when it was first created, so the shortfall/park logic below
        # (which only makes sense for a genuinely NEW reservation) must
        # not run for it. `core.reserve()` itself decides whether this
        # matches (a safe no-op) or conflicts (raises `EventConflict`,
        # caught in `execute()`).
        if self.core.get(delegation_id) is not None:
            outcome = self.core.reserve(event_id, parent_id, delegation_id, agent_id, maximum_usd)
            if outcome == "rejected":
                await self._fail(updater, "reserve rejected")
                return
            # Repair item 4: a matching retry never mints another
            # credential -- minting happens exactly once, only when
            # core.reserve() actually just created the delegation. This
            # also closes the "another reserve-scoped token on the parent
            # guesses an existing child_id to mint itself access" vector,
            # since no mint ever happens here regardless of who is asking.
            original_scopes = self.capabilities.scopes_issued_for(delegation_id)
            if original_scopes is not None and child_scopes != original_scopes:
                await self._fail(
                    updater,
                    "child_scopes does not match this reservation's originally issued "
                    "credential -- changing scopes on a retry is an explicit conflict, "
                    "not a silent re-mint",
                )
                return
            await self._complete_reservation_without_minting(
                delegation_id, parent_id, agent_id, updater
            )
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

        outcome = self.core.reserve(event_id, parent_id, delegation_id, agent_id, maximum_usd)
        if outcome != "created":
            await self._fail(updater, "reserve rejected")
            return

        await self._mint_reservation_credential(
            delegation_id, parent_id, agent_id, child_scopes, authorizer_token_id, updater
        )

    async def _complete_reservation_without_minting(
        self, delegation_id: str, parent_id: str, agent_id: str, updater: TaskUpdater
    ) -> None:
        """Reports a matching reservation retry without minting a new
        credential -- see `_finish_reserve`'s "already exists" branch
        (repair item 4). Credential loss/recovery, if ever supported, must
        be its own explicit rotation operation, not a side effect of this
        path."""
        await updater.add_artifact(
            [
                new_data_part(
                    {
                        "delegation_id": delegation_id,
                        "parent_delegation_id": parent_id,
                        "agent_id": agent_id,
                        "note": "reservation_already_exists",
                        "credential_claim_id": None,
                        "hint": (
                            "this delegation already exists; a matching retry does not "
                            "issue another credential -- use the claim from the original "
                            "reservation"
                        ),
                    }
                )
            ],
            name="receipt",
        )
        await updater.complete()

    async def _mint_reservation_credential(
        self,
        delegation_id: str,
        parent_id: str,
        agent_id: str,
        child_scopes: frozenset[str],
        authorizer_token_id: str,
        updater: TaskUpdater,
    ) -> None:
        token_id, plaintext = self.capabilities.issue(delegation_id, child_scopes)
        # Repair item 5: bound to the EXACT credential that authorized
        # this reservation (authorizer_token_id), not merely to "some
        # currently-valid reserve-scoped credential for parent_id".
        claim_id = self.handoff.create(
            token_id,
            plaintext,
            parent_id,
            "reserve",
            authorizing_token_id=authorizer_token_id,
            child_delegation_id=delegation_id,
        )
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
                            "POST /capabilities/claim on this server with the exact "
                            "bearer token that authorized this reservation, once and "
                            "promptly, to receive the child's capability token. The "
                            "token itself never appears in this A2A task."
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
        """Revokes `delegation_id` and its complete descendant subtree.

        Repair item 4 (race-safe revocation): `core.mark_revocation_started`
        is called FIRST, as its own durable, atomic step, before anything
        that reads which descendants currently exist. From the moment that
        commits, `core.reserve` refuses any new reservation anywhere under
        this delegation (it checks the flag across the whole ancestor
        chain, inside its own transaction) -- so the subtree this method
        goes on to snapshot, settle, and revoke capabilities for is
        guaranteed final; nothing can be added to it after the mark, and
        SQLite's single-writer serialization guarantees nothing that
        committed before the mark can be invisible to the scan performed
        after it. See `core.py`'s module docstring for the full argument.
        """
        delegation_id = payload.get("delegation_id")
        event_id = payload.get("event_id")
        if not delegation_id or not event_id:
            await self._fail(updater, "revoke requires delegation_id, event_id")
            return
        outcome = payload.get("outcome") or "REVOKED"
        self._authorize_revoke(token, delegation_id)

        marked = self.core.mark_revocation_started(delegation_id)
        if not marked:
            await self._fail(updater, "delegation does not exist")
            return

        subtree = self._subtree_ids(delegation_id)
        settled: list[str] = []
        for dep_id in reversed(subtree):  # leaves first, target last
            state = self.core.get(dep_id)
            if state is not None and state.active:
                # Derived purely from dep_id, in the reserved namespace no
                # caller-supplied event_id may use (checked in execute())
                # -- so this can never collide with, or be obstructed by,
                # a caller's own event_id, and a retried revoke recomputes
                # the identical internal event_id every time regardless of
                # what top-level event_id the retry uses (repair item 3).
                internal_event_id = f"{RESERVED_EVENT_ID_PREFIX}revoke-settle:{dep_id}"
                self.core.settle(internal_event_id, dep_id, outcome)
                settled.append(dep_id)
        revoked_token_count = self.capabilities.revoke_for_delegations(subtree)
        purged_claim_count = self.handoff.purge_for_delegations(subtree)

        await updater.add_artifact(
            [
                new_data_part(
                    {
                        "revoked_delegation_ids": subtree,
                        "settled_delegation_ids": settled,
                        "revoked_token_count": revoked_token_count,
                        "purged_claim_count": purged_claim_count,
                    }
                )
            ],
            name="receipt",
        )
        await updater.complete()
