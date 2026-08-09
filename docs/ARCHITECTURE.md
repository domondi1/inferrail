# Inferrail — Architecture

## Component overview

```
src/inferrail/
├── config/      YAML + env -> validated InferrailConfig (pydantic)
├── errors/      Small internal exception hierarchy
├── providers/   Provider protocol + OpenAI-compatible adapter + registry
├── routing/     RoutingContext -> RoutingDecision (static v0.1)
├── telemetry/   InferenceEvent schema + pluggable sinks
├── gateway/     FastAPI app: HTTP schemas, execution engine, routes
└── cli/         `inferrail serve`, `inferrail config check`
```

Each package has one job and depends only on the ones below it in this
list (`gateway` depends on all of them; `errors` depends on nothing). No
package reaches back into `gateway` — `providers`, `routing`, and
`telemetry` are all usable and testable without FastAPI ever being
imported.

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
        +--> reject unsupported features (stream, n != 1) early
        |
        +--> Router.resolve(RoutingContext) -> RoutingDecision
        |         (routing/router.py: static lookup of request.model
        |          in inferrail.yaml's `routes`)
        |
        +--> normalize into NormalizedChatRequest
        |         (provider-agnostic shape: model, messages, sampling
        |          params — see providers/base.py)
        |
        +--> Provider.complete(...), with retry/backoff for errors
        |     marked retryable (providers/openai.py raises normalized
        |     inferrail.errors.* on any failure)
        |
        +--> on success: build ChatCompletionResponse
        |     (OpenAI-shaped, plus a non-standard `inferrail` metadata
        |      block: request id, route, provider, latency, retries)
        |
        +--> emit InferenceEvent to the configured TelemetrySink
        |     (always — on both success and failure)
        |
        v
   HTTP response (200 on success; InferrailError subclasses are caught
   by a FastAPI exception handler in gateway/app.py and mapped to the
   appropriate status code + OpenAI-shaped error body)
```

`InferenceEngine` is deliberately independent of FastAPI — it takes and
returns plain pydantic models — so the full lifecycle above is tested in
`tests/unit/test_gateway.py` via FastAPI's `TestClient`, and the pieces
below it (`Router`, `OpenAIProvider`) are tested standalone with no HTTP
server involved at all.

## The provider boundary

`providers.base.Provider` is a `Protocol` with one method:
`async complete(NormalizedChatRequest, *, timeout) -> NormalizedChatResponse`,
which must raise an `inferrail.errors.ProviderError` subclass (never a raw
`httpx` or provider-SDK exception) on failure.

`OpenAIProvider` is the only implementation in v0.1, but it's generic over
`base_url`: any endpoint that speaks the OpenAI `/chat/completions` shape
(OpenAI itself, Azure OpenAI's compatible surface, vLLM, local
llama.cpp-server, etc.) is usable today just by adding a `providers:` entry
in `inferrail.yaml` with a different `base_url` — no code change. A
provider with a genuinely different wire protocol (e.g. a native
Anthropic or Bedrock client) would get its own module implementing the
same `Provider` protocol; `providers/registry.py` is the one place that
would need a new branch to construct it from config.

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

Every execution — success or failure — produces exactly one
`InferenceEvent` (see `telemetry/events.py`), sent to whatever
`TelemetrySink` is configured (`telemetry/sinks.py`). v0.1 ships
`ConsoleTelemetrySink`, `JSONLTelemetrySink`, and `NullTelemetrySink`. Unknown
values (cost, time-to-first-token) are `None`, never estimated.

The sink is a one-method `Protocol` (`emit(event) -> None`) specifically so
a future sink — SQLite, an OpenTelemetry exporter, or an opt-in Inferrail
Cloud sink — can be added without touching the execution engine. **No sink
in this codebase transmits data off the local machine.**

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
