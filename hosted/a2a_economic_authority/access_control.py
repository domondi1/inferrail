"""Inferrail Economic Authority — A2A surface lockdown (Phase B, repair item 1).

The installed A2A SDK's `RequestHandler` interface exposes eleven standard
methods: `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`,
`CancelTask`, `SubscribeToTask`, and four push-notification-config methods,
plus `GetExtendedAgentCard`. Declaring an HTTP Bearer security scheme on
the Agent Card (`agent_card.py`) does not, by itself, enforce anything --
`DefaultRequestHandlerV2`'s implementations of `GetTask`/`ListTasks`/
`CancelTask`/`SubscribeToTask`/the push-notification-config methods read
or mutate directly against the `TaskStore`, with no per-task ownership
check tying them back to a `delegation_id` or a caller's capability token.
Without this module, any caller who could guess or observe a task_id --
including one with no credential at all -- could fetch another agent's
full task (economic receipts included), enumerate every task on the
server, or cancel someone else's in-flight operation.

Phase B's own design never needs any of these: every operation
(reserve/grant/consume/settle/status/revoke) returns its complete result
synchronously in its own `SendMessage` response, and the one multi-step
flow (`reserve` parking at `TASK_STATE_AUTH_REQUIRED`, then continued by a
`grant` on the same `task_id`) is itself just another `SendMessage` call.
Rather than build and test bespoke per-task-ownership checks for methods
nothing here actually uses, `SendMessageOnlyRequestHandler` removes them
from the reachable surface entirely: every method except `SendMessage`
raises `UnsupportedOperationError`, which the SDK's JSON-RPC dispatcher
turns into a clean JSON-RPC error response, never a 200 with data.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetExtendedAgentCardRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    SendMessageRequest,
    SubscribeToTaskRequest,
    Task,
    TaskPushNotificationConfig,
)
from a2a.utils.errors import UnsupportedOperationError

_DISABLED_MESSAGE = (
    "Phase B of Inferrail Economic Authority supports only the SendMessage "
    "operation. Every other standard A2A method (GetTask, ListTasks, "
    "CancelTask, SubscribeToTask, SendStreamingMessage, push-notification "
    "config, and GetExtendedAgentCard) is explicitly disabled -- there is "
    "no way to read, enumerate, cancel, subscribe to, or alter another "
    "agent's task through this server."
)


def _disabled() -> UnsupportedOperationError:
    return UnsupportedOperationError(_DISABLED_MESSAGE)


class SendMessageOnlyRequestHandler(RequestHandler):
    """Wraps a real `RequestHandler`, exposing only `on_message_send`."""

    def __init__(self, delegate: RequestHandler) -> None:
        self._delegate = delegate

    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Message | Task:
        return await self._delegate.on_message_send(params, context)

    async def on_message_send_stream(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> AsyncGenerator[Message | Task]:
        raise _disabled()
        yield  # pragma: no cover -- unreachable; keeps this an async generator function

    async def on_get_task(self, params: GetTaskRequest, context: ServerCallContext) -> Task | None:
        raise _disabled()

    async def on_list_tasks(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse:
        raise _disabled()

    async def on_cancel_task(
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Task | None:
        raise _disabled()

    async def on_subscribe_to_task(
        self, params: SubscribeToTaskRequest, context: ServerCallContext
    ) -> AsyncGenerator[Message | Task]:
        raise _disabled()
        yield  # pragma: no cover -- unreachable; keeps this an async generator function

    async def on_get_task_push_notification_config(
        self, params: GetTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> TaskPushNotificationConfig:
        raise _disabled()

    async def on_list_task_push_notification_configs(
        self, params: ListTaskPushNotificationConfigsRequest, context: ServerCallContext
    ) -> ListTaskPushNotificationConfigsResponse:
        raise _disabled()

    async def on_create_task_push_notification_config(
        self, params: TaskPushNotificationConfig, context: ServerCallContext
    ) -> TaskPushNotificationConfig:
        raise _disabled()

    async def on_delete_task_push_notification_config(
        self, params: DeleteTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> None:
        raise _disabled()

    async def on_get_extended_agent_card(
        self, params: GetExtendedAgentCardRequest, context: ServerCallContext
    ) -> AgentCard:
        raise _disabled()
