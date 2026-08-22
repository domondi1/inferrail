# 0009. Ambient `task_id` propagation via `contextvars` + an `httpx` hook

## Status

Accepted (experimental — see "Consequences")

## Context

ADR-0008's `TaskTransaction` groups receipts by whatever value a caller
attaches via `X-Inferrail-Attribute-Task-Id`. Attaching that header by
hand — a per-call `extra_headers` dict, `task_id` threaded through every
nested "sub-agent" function's signature purely for Inferrail's benefit,
no way to set it once per client — is real, observed authoring friction
(see the project's internal strategy repository for the dogfooding record
this change is scoped from; not duplicated here, same pattern as
ADR-0008).

Two questions had to be answered before writing code:

1. Does a realistic framework client (not just raw `httpx`) already make
   this easy enough that no Inferrail-side primitive is needed? Tested
   directly against the real `openai` SDK client and against LangChain's
   `ChatOpenAI` (both point `http_client=` at a real client instance
   reused across many calls): neither offers anything beyond a per-call
   header argument or a header fixed at client-construction time — the
   same friction reproduces under both.
2. What's the smallest mechanism that removes it without inventing a
   second transaction system, a broad SDK, or a committed public API?

## Decision

**One primitive: `inferrail.track_task(task_id)`.** A
`contextvars.ContextVar` holds the current task id;
`contextlib.contextmanager` makes `track_task` simultaneously usable as
`with track_task("bug_9281"): ...` and, for free (no extra code —
`contextmanager` objects are already `ContextDecorator`s), as
`@track_task("bug_9281")` on a whole function. No separate async form
exists or is needed: a plain (non-async) context manager works
identically inside `async def` code, and `contextvars` are correctly
scoped per `asyncio.Task` — concurrent interleaved tasks never see each
other's value, verified against real concurrent requests through the
real gateway.

**Setting the contextvar alone touches nothing over HTTP.** Something has
to read it and attach the header to an actual outgoing request.
`attributed_http_client()`/`attributed_async_http_client()` build a plain
`httpx.Client`/`httpx.AsyncClient` with a `request` event hook
(`inject_task_id_header`/`inject_task_id_header_async` — two functions,
not one, because `httpx.AsyncClient` requires an awaitable hook) that
copies the current `track_task` value onto
`X-Inferrail-Attribute-Task-Id` on every request. The caller hands that
client to their SDK's own `http_client=`/`http_async_client=`
constructor argument — verified working, unmodified, against the real
`openai` SDK (`OpenAI`/`AsyncOpenAI`) and against LangChain's
`ChatOpenAI`, neither of which has, needs, or gets any Inferrail-specific
code.

**`task_id` only, not the generic attribute mechanism.** `--attribute-name`
already lets `report`/`transaction` group by any attribute; this
primitive deliberately does not generalize to it — that's unneeded
surface area for the friction actually observed, which is about
`task_id` specifically.

**Zero gateway/schema changes**, same as ADR-0008: `gateway/attribution.py`,
`InferenceReceipt`, and `TaskTransaction` are completely unchanged. This
is a client-side convenience over a mechanism that already exists.

## Consequences

- New module `tracking.py`: `track_task`, `current_task_id`,
  `inject_task_id_header`/`inject_task_id_header_async`,
  `attributed_http_client`/`attributed_async_http_client` — re-exported
  from the top-level `inferrail` package (`import inferrail;
  inferrail.track_task(...)`).
- Only helps httpx-based HTTP clients. A client that doesn't accept a
  custom `httpx.Client`/`AsyncClient` (or doesn't use `httpx` at all) gets
  no benefit from `attributed_http_client`; `track_task`/`current_task_id`
  still work as plain ambient state either way, and a caller can read
  `current_task_id()` to attach the header through whatever mechanism
  their own client exposes.
- **Explicitly experimental — no public API stability commitment.** This
  is the smallest possible test of one ergonomics primitive, not a
  general-purpose Inferrail Python SDK. It may be changed or removed
  based on further real usage, same as every other v0.1 surface per
  `docs/PRODUCT.md`.
- Does not touch non-LLM resource types, outcome/value linkage, or any
  policy/budget enforcement — all remain exactly as scoped (not
  authorized) in ADR-0008.

## Amendment: destination-scoped header injection

The initial implementation attached `X-Inferrail-Attribute-Task-Id` to
*every* request made through an `attributed_http_client`/
`attributed_async_http_client` instance, regardless of destination. That's
a real disclosure risk for a header carrying an internal task identifier:
a caller who reuses the same client instance against a different,
non-Inferrail endpoint (a realistic mistake, not a contrived one — nothing
in either SDK usage pattern this ADR verified against prevents it) would
leak that identifier to it.

Both constructors now require `base_url`, the same URL already passed to
the SDK client's own `base_url=`; the `request` event hook attaches the
header only when a request's scheme+host+port match. No other behavior
described above changes — nesting, restoration, asyncio isolation, and the
zero-gateway-change property are all unaffected, and this remains
explicitly experimental.
