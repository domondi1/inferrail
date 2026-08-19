# 0006. Real SSE streaming and tool-calling passthrough, with an explicit `partial` accounting state

## Status

Accepted

## Context

v0.1 rejected `stream: true` and had no `tools`/`tool_calls`/`role: "tool"`
support at all — a deliberate, documented scope boundary
(docs/PRODUCT.md's non-goals), not an oversight. That boundary made
Inferrail unusable for the workload it most needs to prove itself against:
a real autonomous-agent loop, which is built on tool calling and
overwhelmingly runs over streaming connections.

Adding both raised three questions that had to be answered before writing
code, all with the same underlying tension: Inferrail is a gateway, not an
agent runtime, and the existing economic-correctness invariants (never
fabricate cost, never silently double-execute observable work) don't just
apply to the atomic non-streaming path they were designed for — they have
to survive a request that can now fail *after* it has already started
producing externally-visible output.

1. Does supporting tool calls mean Inferrail interprets them in any way?
2. What does "retry" mean once a response is no longer atomic?
3. What does an `InferenceEvent`/`InferenceReceipt` mean for a stream that
   started successfully but never finished?

## Decision

**Protocol fidelity, not interpretation.** Inferrail transports tool
calls; it never executes one. A tool call's `function.arguments` is
carried as an opaque `str` everywhere in this codebase — from
`providers/openai.py`'s response parsing through `ChatCompletionResponse`
— and is never `json.loads`'d and re-dumped, which could reorder keys or
reformat numbers and silently change bytes a tool-execution step depends
on matching exactly. The streaming path goes further: `_iter_stream`
(`gateway/execution.py`) forwards every upstream SSE byte completely
unmodified, via a raw-bytes passthrough rather than reconstructing and
re-emitting parsed events — a side-channel bookkeeper reads the same bytes
only to recover the final `usage` block for accounting, and never gates or
alters what's actually sent to the client.

**The retry boundary is "has any byte reached the client yet," not "did
the HTTP call succeed."** Streaming is split into two phases specifically
to make this a structural property rather than a runtime check:
`prepare_stream` is a plain coroutine that resolves routing and opens the
upstream connection through to its first chunk, retrying only failures
discovered before that point; `_iter_stream` is the actual generator handed
to the ASGI response, and there is no code path from it back into a retry
loop. Once `prepare_stream` returns, retrying is no longer possible by
construction, not by convention — a provider failure or a client
disconnect from that point on ends the stream instead.

**A new `status: "partial"`, not a reinterpretation of `"error"`.**
`InferenceEvent`/`InferenceReceipt` (`telemetry/events.py`,
`receipts/schema.py`) gain a third status value for exactly the case above:
some output was already observed by the client, but the request never
reached a clean, fully-measured completion. This is deliberately a new
enum value rather than folding it into `"error"`, so `inferrail report`
and any future analysis can distinguish "never worked" from "worked
partially then broke" — a materially different signal for debugging a
production agent deployment. A `partial` record never fabricates cost: it
carries whatever usage was actually observed (`None` unless the provider's
final usage chunk happened to arrive right before the failure), and
`receipts/builder.py`'s existing "price only when both token counts are
known" rule already makes that null-cost behavior fall out for free,
without any special-casing.

**Cancellation closes the upstream connection immediately, not
eventually.** `async for` does not close the iterable it loops over when
interrupted — only a fully-consumed loop reaches `StopAsyncIteration` on
its own. `Provider.stream()` is typed as `AsyncGenerator[bytes, None]`
(not the broader `AsyncIterator[bytes]`) specifically so `_iter_stream`'s
`finally` block can call `.aclose()` on it explicitly on every exit path,
so an abandoned upstream connection is torn down the moment the client
disconnects rather than left for garbage collection to eventually clean
up. One narrow, documented limitation remains: Python's generator
semantics make `.aclose()` on a *never-started* generator a no-op — see
`gateway/execution.py`'s module docstring and
`tests/unit/test_streaming.py`.

## Consequences

- `ChatMessage`/`NormalizedChatRequest`/`NormalizedChatResponse`
  (`providers/base.py`) and `ChatCompletionRequest`/
  `ChatCompletionChoiceMessage` (`gateway/schemas.py`) all gain
  `tools`/`tool_choice`/`parallel_tool_calls`/`tool_calls`/`tool_call_id`
  fields. `tools`/`tool_choice` stay loosely typed (`dict[str, object]`
  passthrough) rather than a strict nested schema, so Inferrail can never
  drop or reorder a tool-definition field it doesn't itself know about.
- `stream_options` is a new passthrough field: Inferrail auto-injects
  `{"include_usage": true}` for a verified `type: openai` provider (the
  same gate `PricingResolver` uses, ADR-0005) when the caller didn't
  already set it, but a caller's own value always wins.
- `n != 1` remains rejected exactly as before — this ADR only removes the
  `stream`/tool-call boundaries, not the others documented in
  docs/PRODUCT.md.
- `inferrail report`'s aggregation (`cli/report.py`) needed no code change
  for the new status value: a `partial` receipt already falls into
  "not successful, not an unknown-cost pricing gap" under the existing
  logic, which is the correct bucket for it.
- Every `Provider` implementation (`OpenAIProvider`, the CLI demo's fake
  provider, test doubles) must now implement `stream()`, not just
  `complete()` — enforced structurally by the `Provider` Protocol.
