"""Ambient `task_id` propagation for callers of the Inferrail gateway.

Every request belonging to one task must carry the same
`X-Inferrail-Attribute-Task-Id` header (see `gateway/attribution.py`,
`transactions/builder.py`) for `inferrail transaction <task-id>` to group
it correctly. Attaching that header by hand on every call site --
threading `task_id` through every nested function's signature purely for
Inferrail's benefit -- is real, observed friction. See
`docs/adr/0009-ambient-task-tracking.md` for the full rationale.

`track_task` sets a `contextvars.ContextVar` for the duration of a `with`
block (or a whole decorated function -- `contextlib.contextmanager`
objects are `ContextDecorator`s for free, so `@track_task(...)` and
`with track_task(...):` are the same primitive, not two). No async
variant exists or is needed: a plain, non-async context manager works
identically inside `async def` code, and `contextvars` are correctly
scoped per `asyncio.Task` -- interleaved concurrent tasks never see each
other's value.

Setting the contextvar alone does nothing over HTTP by itself --
something has to read it and attach the header to an actual outgoing
request. `attributed_http_client`/`attributed_async_http_client` build an
`httpx.Client`/`httpx.AsyncClient` with a `request` event hook that does
exactly that, for handing to any httpx-based SDK client's own
`http_client=`/`http_async_client=` constructor argument.

v0.1 of this primitive: `task_id` only, not the generic
`X-Inferrail-Attribute-<Name>` mechanism `--attribute-name` exposes
elsewhere. No change to the gateway's request path, `InferenceReceipt`,
or `TaskTransaction`. Experimental -- no public API stability commitment,
same as every other v0.1 surface (see `docs/PRODUCT.md`).
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

_TASK_ID_HEADER = "X-Inferrail-Attribute-Task-Id"

_current_task_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "inferrail_current_task_id", default=None
)


@contextmanager
def track_task(task_id: str) -> Iterator[None]:
    """Attach `task_id` to every Inferrail-attributed HTTP request made,
    directly or by any nested call, for the duration of this block.

    Usable as a context manager (`with track_task("bug_9281"): ...`) or,
    identically, as a decorator (`@track_task("bug_9281")`) on a whole
    function.

    Requires an httpx-based client built with `attributed_http_client`/
    `attributed_async_http_client`, or any httpx client that installs
    `inject_task_id_header`/`inject_task_id_header_async` itself as a
    `request` event hook -- setting this alone does not touch a client
    that wasn't built that way.
    """
    if not task_id:
        raise ValueError("task_id must be a non-empty string")
    token = _current_task_id.set(task_id)
    try:
        yield
    finally:
        _current_task_id.reset(token)


def current_task_id() -> str | None:
    """The `task_id` set by the innermost enclosing `track_task`, or
    `None` outside of one."""
    return _current_task_id.get()


def inject_task_id_header(request: httpx.Request) -> None:
    """`httpx` `request` event hook: attach the current `track_task`
    task id, if any, as `X-Inferrail-Attribute-Task-Id`.

    Sync clients only -- see `inject_task_id_header_async` for
    `httpx.AsyncClient`, which requires an awaitable hook.
    """
    task_id = _current_task_id.get()
    if task_id is not None:
        request.headers[_TASK_ID_HEADER] = task_id


async def inject_task_id_header_async(request: httpx.Request) -> None:
    """`async` counterpart of `inject_task_id_header`, for
    `httpx.AsyncClient`."""
    inject_task_id_header(request)


def attributed_http_client() -> httpx.Client:
    """An `httpx.Client` that ambiently attaches the current
    `track_task` task id to every outgoing request. Pass as
    `http_client=` to any httpx-based SDK client (e.g. `openai.OpenAI`,
    `langchain_openai.ChatOpenAI`)."""
    return httpx.Client(event_hooks={"request": [inject_task_id_header]})


def attributed_async_http_client() -> httpx.AsyncClient:
    """`async` counterpart of `attributed_http_client`, for
    `openai.AsyncOpenAI` and similar async clients."""
    return httpx.AsyncClient(event_hooks={"request": [inject_task_id_header_async]})
