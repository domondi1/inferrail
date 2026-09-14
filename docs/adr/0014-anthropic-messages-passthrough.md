# 0014. A parallel, wire-native `/v1/messages` passthrough for Anthropic

## Status

Accepted

## Context

`MISSION.md`'s v0.3.0 calls for "Anthropic `/v1/messages` passthrough
with streaming + tool use, priced via the catalog — this makes 'point
Claude Code at Inferrail' true." Claude Code and the official Anthropic
SDKs call `POST /v1/messages`, not `POST /v1/chat/completions` — so this
cannot be satisfied by adding another `Provider` behind the existing
OpenAI-shaped pipeline (`providers.base.NormalizedChatRequest`,
`gateway.schemas.ChatCompletionRequest`, `gateway.execution.
InferenceEngine`). Inferrail must accept and answer Anthropic's own wire
format directly.

The two wire formats differ structurally, not just cosmetically:
Anthropic's `system` is a top-level request field, not a message with
`role: "system"`; message `content` is commonly an array of typed blocks
(`text`, `tool_use`, `tool_result`, ...) rather than a plain string;
`tool_use` (the assistant requesting a tool) and `tool_result` (a
`user`-role message answering it) replace OpenAI's `tool_calls`/
`role: "tool"` shape; and the streaming SSE protocol is a completely
different event sequence (named `event: message_start` /
`content_block_start` / `content_block_delta` / `content_block_stop` /
`message_delta` / `message_stop` events, each carrying a matching
`type` in its `data:` JSON) with usage split across two events
(`message_start.message.usage.input_tokens`, then a *cumulative*
`message_delta.usage.output_tokens`) — nothing like OpenAI's single
`data: {"usage": {...}}` chunk near the end.

ADR-0006 already established this repo's non-negotiable streaming
principle: upstream bytes are proxied byte-for-byte, never buffered and
re-chunked, with usage recovered by a side-channel parser that never
gates or alters what's forwarded. Translating an Anthropic-shaped
client's request into OpenAI's normalized shape and back — the only way
to reuse `InferenceEngine` as-is — would mean either giving up true
byte-for-byte streaming fidelity for Anthropic traffic, or building a
bidirectional SSE-format transcoder, which is a much larger and riskier
undertaking than the mission's own framing ("passthrough") implies.

## Decision

**A second, parallel wire-native pipeline, not a translation layer.**
`POST /v1/messages` is its own route, with its own request/response
schemas (`gateway/anthropic_schemas.py`), its own normalized types and
provider contract (`providers/anthropic_base.py`), its own concrete
adapter (`providers/anthropic.py`'s `AnthropicProvider`, hitting real
`api.anthropic.com` — or an `anthropic_compatible` endpoint, mirroring
`openai`/`openai_compatible`), and its own engine
(`gateway/anthropic_execution.py`'s `AnthropicInferenceEngine`) that
copies `InferenceEngine`'s retry/cancellation/receipt/telemetry
structure but speaks Anthropic's events. `docs/PRINCIPLES.md`'s
"provider neutrality" ("a genuinely different protocol gets its own
adapter behind the same interface") is honored at the level of pattern —
a small `Protocol` boundary between engine and adapter — not by forcing
one literal request/response type onto a wire format it can't represent
without loss; `providers/anthropic_base.py`'s `AnthropicMessagesProvider`
is that Protocol for this wire format, exactly as `providers.base.
Provider` is for OpenAI's. `InferenceEngine` and `AnthropicInferenceEngine`
are deliberately not unified behind one generic base class merely
because their control flow rhymes — "small, simple hot path" (same doc)
argues against that abstraction until a third wire format actually needs
it.

**Content and tool blocks are passthrough, not modeled block-by-block.**
`AnthropicMessage.content: str | list[dict[str, object]]` accepts
whatever block array a caller sends (or a real Anthropic backend
returns) without a typed `TextBlock`/`ToolUseBlock`/... hierarchy —
exactly the same choice `NormalizedChatRequest.tools` already made for
OpenAI, for the same reason: Inferrail must never drop or reorder a
block-shaped field it doesn't itself interpret. This also means "tool
use" needs no special-case code on top of ordinary passthrough — a
`tool_use`/`tool_result` block just flows through as part of `content`.

**Only `id`/`model` are ever overridden, and only in the non-streaming
response** — the same rule ADR-0007-era `docs/PRODUCT.md` already
documents for `/v1/chat/completions`: `id` becomes Inferrail's own
`request_id` and `model` becomes the resolved route target, so a caller
can correlate a response with its receipt without a second lookup; the
provider's own `id`/`model` survive in the additive `inferrail`
metadata block (`raw_model`, reusing the already wire-format-agnostic
`gateway.schemas.InferrailMetadata`). A streaming response is never
touched this way — full protocol fidelity, matching the OpenAI route
exactly.

**Shared infrastructure stays shared.** `routing.router.Router`,
`pricing.resolver.PricingResolver`, `receipts.sinks.ReceiptSink`, and
`telemetry.sinks.TelemetrySink` are all already provider/format-agnostic
(keyed by provider name and model string, nothing OpenAI-specific) and
are reused as-is — one `routes:` section in `inferrail.yaml` serves
both wire formats; a route naming an `anthropic`-typed provider only
makes sense reached via `/v1/messages`, exactly as a route naming an
`openai`-typed provider only makes sense via `/v1/chat/completions`.
`providers.registry.build_providers` (OpenAI-shaped) and the new
`build_anthropic_providers` each build only their own provider type from
the same `config.providers` dict, so a provider entry is simply invisible
to the engine that can't use it — never a hard error at startup for
having "the wrong kind" of provider configured for the other pipeline.

**Pricing gate generalizes, not duplicates.** `PricingResolver.resolve`
now looks up a small `{"openai": ..., "anthropic": ...}` table by the
provider's own verified `type` instead of hardcoding `type == "openai"`
— the "only a verifiably real vendor endpoint with no overridden
`base_url` gets built-in pricing" rule (originally ADR-0005's) applies
identically to `anthropic`, and an `anthropic_compatible` endpoint never
gets Anthropic's prices for the same reason `openai_compatible` never
gets OpenAI's. `pricing/builtin_anthropic.py` is a new, separate,
independently-verified catalog (checked against
`platform.claude.com/docs/en/about-claude/models/overview` on
2026-09-14) — kept apart from `pricing/builtin.py` rather than merged
into one file, so each catalog's own verification provenance stays
unambiguous.

## Consequences

- An operator who never configures an `anthropic`-typed provider sees no
  behavior change at all — `/v1/messages` simply has no reachable
  provider for any route, failing exactly like an unconfigured OpenAI
  route does today (a clear `RoutingError`, not a crash).
- Claude Code (or any Anthropic-SDK-based client) can now point its
  `base_url` at a running Inferrail instance and get real streaming,
  tool use, and a payload-free receipt for every call — the literal
  acceptance bar this unit was scoped against.
- Two engines now exist side by side. Future shared engine behavior
  (e.g. budget enforcement, unit (3)) must be added to both, deliberately
  — there is no single choke point today. If a third wire format is ever
  added, revisit whether a shared retry/telemetry/receipt-emission
  helper (not a shared request/response type) is worth extracting; two
  data points aren't enough to justify that abstraction yet.
- `inferrail report`/`transaction`/`work` need no changes: receipts from
  both pipelines land in the same sink, already keyed only by
  provider/model/status/attributes — wire format was never part of that
  shape.
