# Inferrail — Architecture

## Component overview

```
src/inferrail/
├── config/      YAML + env -> validated InferrailConfig (pydantic), plus
│                an in-memory quickstart config builder (same type/validation)
├── errors/      Small internal exception hierarchy
├── providers/   Provider protocol + OpenAI-compatible adapter + registry
├── routing/     RoutingContext -> RoutingDecision (static v0.1)
├── telemetry/   InferenceEvent schema + pluggable sinks
├── pricing/     Built-in + operator-override price catalog, PricingResolver
├── receipts/    InferenceReceipt schema, Decimal cost calculator, sinks
├── gateway/     FastAPI app: HTTP schemas, execution engine, routes,
│                attribution header parsing
└── cli/         `inferrail serve` (+ `--quickstart`), `inferrail config
                 check`, `inferrail report`, `inferrail demo`, `inferrail try`
```

Each package has one job and depends only on the ones below it in this
list (`gateway` depends on all of them; `errors` depends on nothing).
`pricing` and `receipts` depend only on `config`/`errors`, the same
dependency shape as `providers`/`routing`/`telemetry`. No package reaches
back into `gateway` — everything below it is usable and testable without
FastAPI ever being imported.

## Request lifecycle

```
POST /v1/chat/completions (gateway/routes.py)
        |
        v
ChatCompletionRequest validation (gateway/schemas.py, pydantic)
        |
        v
InferenceEngine.execute (gateway/execution.py)
        |
        +--> reject unsupported features (n != 1) early
        |
        +--> Router.resolve(RoutingContext) -> RoutingDecision
        |         (routing/router.py: static lookup of request.model
        |          in inferrail.yaml's `routes`)
        |
        +--> normalize into NormalizedChatRequest
        |         (provider-agnostic shape: model, messages, sampling
        |          params, tools/tool_choice/parallel_tool_calls — see
        |          providers/base.py)
        |
        +--> Provider.complete(...), with retry/backoff for errors
        |     marked retryable (providers/openai.py raises normalized
        |     inferrail.errors.* on any failure)
        |
        +--> on success: build ChatCompletionResponse
        |     (OpenAI-shaped, incl. tool_calls when present, plus a
        |      non-standard `inferrail` metadata block: request id,
        |      route, provider, latency, retries)
        |
        +--> emit InferenceEvent to the configured TelemetrySink
        |     (always — on both success and failure)
        |
        +--> build + emit InferenceReceipt to the configured ReceiptSink
        |     (always — on both success and failure; see below)
        |
        v
   HTTP response (200 on success; InferrailError subclasses are caught
   by a FastAPI exception handler in gateway/app.py and mapped to the
   appropriate status code + OpenAI-shaped error body)
```

### Streaming (`stream: true`)

`chat_completions` branches on `payload.stream` and follows a different,
two-phase path through `InferenceEngine` instead of `execute`:

```
InferenceEngine.prepare_stream (a plain coroutine, not a generator)
        |
        +--> reject unsupported features, resolve routing (same as above)
        |
        +--> open Provider.stream(...), retrying only a failure discovered
        |     before the first chunk arrives — this is the retry boundary:
        |     once this coroutine returns, no further retry can happen,
        |     by construction. Still raises a normal InferrailError on
        |     total failure, caught by the same exception handler as the
        |     non-streaming path, since nothing has reached the HTTP
        |     client yet.
        |
        v
   gateway/routes.py wraps the result in StreamingResponse(...) — only
   now does a 200 and any bytes reach the client
        |
        v
InferenceEngine._iter_stream (the actual async generator StreamingResponse
consumes)
        |
        +--> forwards every remaining upstream byte completely unmodified
        |     (raw SSE passthrough — never reparses or reorders tool-call
        |     argument fragments or any other content)
        |
        +--> a side-channel bookkeeper reads the same bytes only to
        |     recover the final `usage` block for accounting — it never
        |     gates or alters what's forwarded
        |
        +--> on clean completion: emit status="success", using whatever
        |     usage was actually observed
        |
        +--> on a provider failure or client disconnect (GeneratorExit)
        |     after at least one chunk was already yielded: emit
        |     status="partial", explicitly closing the upstream
        |     connection — never retried, since retrying here would
        |     silently replay already-observed agent execution
        |
        +--> on a provider failure with zero chunks yielded: emit
              status="error", same as a non-streaming failure
```

See `gateway/execution.py`'s module docstring for the full design
rationale, including the one documented narrow limitation (a disconnect
detected before the generator is ever driven at all is a no-op per
Python's own generator semantics — see that docstring and
`tests/unit/test_streaming.py`), and
`docs/adr/0006-streaming-and-tool-calling-execution-fidelity.md` for why
these specific boundaries were chosen.

`gateway/attribution.py` extracts caller-supplied `X-Inferrail-Attribute-*`
headers into a `dict[str, str]` before `InferenceEngine.execute` is
called, and that dict is threaded through unchanged to wherever a receipt
is built — it never enters `ChatCompletionRequest`/`NormalizedChatRequest`,
so it cannot reach a provider.

Receipt assembly (`receipts/builder.py`) is a small, separate step from
both retry/telemetry and from `InferenceEngine` itself: given token usage
(or `None`, on failure), it asks `PricingResolver.resolve(provider, model)`
for a verified price, and if one exists, `receipts/calculator.py` computes
a `Decimal` cost. Either lookup returning nothing leaves the receipt's
`pricing`/`estimated_cost_usd` as `None` — never a fabricated cost.

`InferenceEngine` is deliberately independent of FastAPI — it takes and
returns plain pydantic models — so the full lifecycle above is tested in
`tests/unit/test_gateway.py` via FastAPI's `TestClient`, and the pieces
below it (`Router`, `OpenAIProvider`) are tested standalone with no HTTP
server involved at all.

That independence is also what makes `inferrail try` (`cli/try_cmd.py`)
possible without a second inference stack: it builds the exact same
`InferenceEngine`, from an in-memory quickstart `InferrailConfig`
(`config/quickstart.py`), and calls `execute` directly — no FastAPI
`TestClient`, no HTTP round-trip, no server process. The HTTP layer
(`gateway/routes.py`, `gateway/app.py`) is one adapter in front of
`InferenceEngine`; the CLI is another. `inferrail demo` (`cli/demo.py`)
follows the same shape one level further out, swapping in a fake
in-memory `Provider` instead of `OpenAIProvider` so it needs neither a key
nor the network, while still going through the real `Router`,
`PricingResolver`, receipt assembly, and `ReceiptSink`.

## The provider boundary

`providers.base.Provider` is a `Protocol` with two methods:
`async complete(NormalizedChatRequest, *, timeout) -> NormalizedChatResponse`
for the non-streaming path, and
`stream(NormalizedChatRequest, *, timeout) -> AsyncGenerator[bytes, None]`
for the streaming path — an async generator, not `AsyncIterator`,
specifically so the engine can rely on `.aclose()` to tear down an
abandoned upstream connection immediately on cancellation (see
`gateway/execution.py`). Both must raise an `inferrail.errors.ProviderError`
subclass (never a raw `httpx` or provider-SDK exception) for any failure
discovered before the first byte/chunk; a `stream()` failure discovered
*after* it has already yielded something simply propagates as a plain
exception out of the generator, which the engine treats as terminal
(never retried).

`OpenAIProvider` is the only implementation in v0.1, but it's generic over
`base_url`: any endpoint that speaks the OpenAI `/chat/completions` shape
(OpenAI itself, Azure OpenAI's compatible surface, vLLM, local
llama.cpp-server, etc.) is usable today just by adding a `providers:` entry
in `inferrail.yaml` with a different `base_url` — no code change. A
provider with a genuinely different wire protocol (e.g. a native
Anthropic or Bedrock client) would get its own module implementing the
same `Provider` protocol; `providers/registry.py` is the one place that
would need a new branch to construct it from config.

`OpenAIProvider.stream()` auto-injects `stream_options: {"include_usage":
true}` when the caller didn't already set it, but only for a provider
verifiably running OpenAI's own API (`type: openai`, default `base_url`)
— the same gate `pricing.resolver.PricingResolver` uses (see ADR-0005) —
since an `openai_compatible` endpoint is never assumed to support an
OpenAI-specific extension it never advertised. Without that final usage
chunk, a streaming receipt simply leaves cost `null`, exactly like any
other unresolvable-usage case; it never blocks the stream itself.

## The routing boundary

`routing.router.Router.resolve(RoutingContext) -> RoutingDecision` is a
pure function of `inferrail.yaml`'s `routes:` section today: the
request's `model` field is a route name, looked up directly. Everything
downstream of `RoutingDecision` (provider name, target model, retry count,
timeout) doesn't know or care how the decision was made.

That split is intentional: adding real routing intelligence later (cost
ceilings, latency budgets, allowed-provider lists, capability matching,
historical-reliability-aware selection) means enriching `RoutingContext`
and replacing the body of `resolve` — or introducing a `RoutingPolicy`
abstraction with multiple strategies — without touching `InferenceEngine`
or anything provider-related.

## The telemetry boundary

Every execution — success, failure, or a stream interrupted partway
through (`status: "partial"` — see the streaming section above) —
produces exactly one `InferenceEvent` (see `telemetry/events.py`), sent to
whatever `TelemetrySink` is configured (`telemetry/sinks.py`). v0.1 ships
`ConsoleTelemetrySink`, `JSONLTelemetrySink`, and `NullTelemetrySink`. Unknown
values (cost, time-to-first-token) are `None`, never estimated — a
`partial` record carries only whatever usage was actually observed before
the interruption, which is `None` unless the provider's final usage chunk
happened to arrive right before the failure.

The sink is a one-method `Protocol` (`emit(event) -> None`) specifically so
a future sink — SQLite, an OpenTelemetry exporter, or an opt-in Inferrail
Cloud sink — can be added without touching the execution engine. **No sink
in this codebase transmits data off the local machine.**

## The pricing and receipts boundary

`pricing.resolver.PricingResolver.resolve(provider_name, model) ->
PriceEntry | None` is a pure function of `inferrail.yaml` (its `providers:`
and `pricing:` sections) — no runtime state, mirroring `Router.resolve`.
It checks an operator override first, then a small built-in catalog
(`pricing/builtin.py`) gated to providers verifiably running OpenAI's own
API (`type: openai`, default `base_url`) — see
`docs/adr/0005-privacy-preserving-economic-receipts.md` for why that gate
exists. Anything it can't resolve is `None`.

`InferenceReceipt` (`receipts/schema.py`) is deliberately a separate type
from `InferenceEvent`, not an extension of it — see ADR 0005. Like
`InferenceEvent`, it has no field capable of holding prompt or response
content (`test_inference_receipt_has_no_payload_fields`), and one
additional intentional exception: caller-supplied `attributes` **are**
persisted, since they're business metadata the caller explicitly declared,
not extracted from the prompt. `ReceiptSink` (`receipts/sinks.py`) is a
one-method `Protocol`, same shape as `TelemetrySink`, with a JSONL and a
null implementation in v0.1 — nothing here transmits data off the machine
either.

`inferrail report` (`cli/report.py`) reads that JSONL file back, tolerant
of malformed or older-schema rows (skipped, not fatal), and aggregates by
provider, model, route, or any attribution attribute name — pure functions
independent of `argparse`, mirroring how `InferenceEngine` stays
independent of FastAPI.

## OSS data plane vs. future hosted control plane

Everything in this repository is the **data plane**: the hot path that
actually serves inference requests. It is designed to keep running with no
dependency on any Inferrail-operated service — there is currently no code
path that calls out to one.

A **control plane** (fleet-wide analytics, historical provider/model
performance comparison, policy management across many deployments,
collaborative dashboards, alerting) is explicitly *not* built here, but
the boundaries are shaped so it could be added later without invasive
rework:

- `TelemetrySink` is an interface a remote sink could implement later —
  today it has no remote implementation.
- `RoutingDecision` is already a self-contained value; a future
  `RoutingPolicy` could be informed by data from a control plane without
  changing what `InferenceEngine` consumes.
- Config loading (`config/loader.py`) reads only a local YAML file. A
  future centralized-config mode would be an alternative config *source*
  behind the same `InferrailConfig` type, not a rewrite of anything that
  consumes it.

The test for whether a future feature belongs in the OSS data plane or a
hosted control plane: does its value come from a single request/process
(data plane), or from aggregating many requests/processes over time
(naturally hosted)? See `docs/adr/0004-data-plane-control-plane-boundary.md`.

## Performance

Two latency components matter and are currently distinguishable in
principle even though v0.1 doesn't yet report them separately in the API
response: `InferenceEvent.total_latency_ms` is measured from the moment
`InferenceEngine.execute` starts (i.e. it includes Inferrail's own request
normalization and any retry backoff) to when a result is available.
Isolating pure "Inferrail overhead" from "time spent waiting on the
provider" would require timing the `provider.complete()` call itself
separately — a natural, low-risk follow-up once there's a reason to
optimize against it. No performance claims are made in this codebase
without a benchmark to back them.
