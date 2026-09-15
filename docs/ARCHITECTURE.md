# Inferrail — Architecture

## Component overview

```
src/inferrail/
├── config/      YAML + env -> validated InferrailConfig (pydantic), plus
│                an in-memory quickstart config builder (same type/validation)
├── errors/      Small internal exception hierarchy
├── appdata.py   OS-conventional per-user app-data directory (stdlib
│                only) — used only by `inferrail serve --app-mode`
├── providers/   Provider protocols + OpenAI-compatible and
│                Anthropic-compatible adapters (parallel, not shared —
│                see docs/adr/0014) + registry
├── routing/     RoutingContext -> RoutingDecision (static v0.1)
├── telemetry/   InferenceEvent schema + pluggable sinks
├── pricing/     Built-in (OpenAI + Anthropic) + operator-override price
│                catalogs, PricingResolver
├── receipts/    InferenceReceipt schema, Decimal cost calculator, sinks
├── budgets/     Budget schema, SQLite store, pre-flight/post-flight
│                enforcement (see docs/adr/0015) — depends on receipts
│                (ReceiptsStore.query()) and pricing (PricingResolver)
├── localapi/    `/v1/local/*` — the local control API (see
│                docs/adr/0016), mounted only under `--app-mode`;
│                depends on receipts, budgets, and work (all read-only
│                or CRUD reuse of those packages' own domain models)
├── usage_ping/  Opt-in, anonymous usage ping (docs/adr/0019) — wraps a
│                ReceiptSink, never reuses TelemetrySink/ReceiptSink
│                itself; the one deliberate exception to "nothing
│                transmits data off the machine by default"
├── dashboard.py Locates a built `app/dist` (see docs/adr/0017); no
│                dependency on anything else in this package
├── gateway/     FastAPI app: HTTP schemas + execution engine for both
│                /v1/chat/completions and /v1/messages, routes,
│                attribution header parsing; mounts `localapi.routes`
│                and the dashboard static build when
│                `create_app(..., app_mode=True)`

app/             Dashboard SPA (React + Vite + TypeScript, docs/adr/0017)
│                — a separate Node/npm project, built to `app/dist`;
│                zero dependency in either direction from `src/inferrail`
│                except the runtime discovery above
├── transactions/ TaskTransaction schema + read-side builder over receipts
├── tracking.py  Client-side helper: ambient task_id propagation
│                (contextvars + an httpx event hook) for callers of the
│                gateway — experimental, see docs/adr/0009
└── cli/         `inferrail serve` (+ `--quickstart`/`--app-mode`),
                 `inferrail config check`, `report`, `transaction`,
                 `demo`, `try`, `budget set|list|rm`, `pricing update`,
                 `doctor`, `telemetry preview|status|enable|disable`
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
        +--> BudgetEnforcer.check(...) — no-op unless budgets.enabled
        |         (budgets/enforcement.py: pre-flight upper-bound
        |          estimate + spent-so-far vs. every matching budget;
        |          raises BudgetExceededError for an exceeded
        |          block-mode budget, *before* the provider is ever
        |          contacted — see docs/adr/0015)
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

`OpenAIProvider` is the only implementation of `Provider` for `/v1/chat/
completions`, but it's generic over `base_url`: any endpoint that speaks
the OpenAI `/chat/completions` shape (OpenAI itself, Azure OpenAI's
compatible surface, vLLM, local llama.cpp-server, etc.) is usable today
just by adding a `providers:` entry in `inferrail.yaml` with a different
`base_url` — no code change.

`OpenAIProvider.stream()` auto-injects `stream_options: {"include_usage":
true}` when the caller didn't already set it, but only for a provider
verifiably running OpenAI's own API (`type: openai`, default `base_url`)
— the same gate `pricing.resolver.PricingResolver` uses (see ADR-0005) —
since an `openai_compatible` endpoint is never assumed to support an
OpenAI-specific extension it never advertised. Without that final usage
chunk, a streaming receipt simply leaves cost `null`, exactly like any
other unresolvable-usage case; it never blocks the stream itself.

A provider with a genuinely different wire protocol does **not** get
squeezed into `Provider`/`NormalizedChatRequest` — see the Anthropic
boundary below, and docs/adr/0014-anthropic-messages-passthrough.md for
why a real wire-format difference gets its own parallel `Protocol`
instead.

## The Anthropic Messages boundary

`providers.anthropic_base.AnthropicMessagesProvider` is the `/v1/messages`
analog of `Provider` — same two-method shape
(`complete`/`stream`), same `ProviderError` failure contract — but over
`AnthropicNormalizedRequest`/`AnthropicNormalizedResponse`, which are not
interchangeable with the OpenAI-shaped types: `system` is a top-level
field, message `content` is a passthrough string-or-block-array (never a
typed block hierarchy — a `tool_use` block's `input` is a JSON *object*,
unlike OpenAI's string-typed `FunctionCall.arguments`), and there is no
`tool_calls` field to reconstruct, since content blocks (including
`tool_use`) already flow through as part of `content`.

`AnthropicProvider` is the only implementation, generic over `base_url`
the same way `OpenAIProvider` is (`type: anthropic`/`anthropic_compatible`)
— it authenticates with `x-api-key`/`anthropic-version` instead of
`Authorization: Bearer`, the one real difference at the HTTP layer.
`gateway.anthropic_execution.AnthropicInferenceEngine` is a second,
parallel `InferenceEngine` — deliberately not unified with it — reading
usage from Anthropic's own SSE event sequence
(`message_start`/`message_delta`, cumulative `output_tokens`) instead of
OpenAI's single trailing usage chunk. Both engines share the same
`Router`, `PricingResolver`, `ReceiptSink`, and `TelemetrySink` instances
(see `gateway/app.py:create_app`) — routing, pricing, and the receipt
ledger were already provider/format-agnostic.

## The routing boundary

`routing.router.Router.resolve(RoutingContext) -> RoutingDecision` is a
pure function of `inferrail.yaml`'s `routes:` section today: the
request's `model` field is a route name, looked up directly. Everything
downstream of `RoutingDecision` (provider name, target model, retry count,
timeout) doesn't know or care how the decision was made.

If a route lookup misses and `default_provider` is configured,
`resolve` falls back to forwarding `model` unchanged to that provider
(`RoutingDecision.route_name` set to the fixed sentinel `"passthrough"`)
instead of raising — see `docs/adr/0007-model-passthrough-routing.md`.
This is still the same pure function of config; `InferenceEngine` still
only ever consumes a `RoutingDecision` and has no branch for how it was
produced.

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
one-method `Protocol`, same shape as `TelemetrySink`, with JSONL, a
WAL-mode SQLite store (`receipts/sqlite_store.py`, indexed on
`ts`/`work_id`/`project`/`model` — see docs/adr/0013), and a null
implementation — nothing here transmits data off the machine either.

`inferrail report` (`cli/report.py`) reads receipts back — from whichever
sink produced the target file, detected by content rather than a flag —
tolerant of malformed or older-schema rows (skipped, not fatal), and
aggregates by provider, model, route, or any attribution attribute name —
pure functions independent of `argparse`, mirroring how `InferenceEngine`
stays
independent of FastAPI.

## The budgets boundary

See `docs/adr/0015-budget-enforcement.md` for the full design. In short:
`budgets.store.BudgetStore` (SQLite, same connection discipline as
`ReceiptsStore`) holds `Budget` rows (scope/scope_value/window/mode/
limit_usd), managed by `inferrail budget set|list|rm` independent of
whether enforcement is on. `budgets.enforcement.BudgetEnforcer` — wired
into both `InferenceEngine` and `AnthropicInferenceEngine` only when
`InferrailConfig.budgets.enabled` — does two things: `check()` (called
after routing resolves the provider/model, before any provider call)
projects `spent_so_far` (via `ReceiptsStore.query()`) plus a catalog-based
upper-bound estimate against every matching budget, raising
`BudgetExceededError` for a `block`-mode budget that would be exceeded;
`augment_overrun()` (called once actual usage is known, right before the
receipt is emitted) adds a `budget_overrun_usd` attribute when the
*actual* cost pushes a budget over its limit. Neither method is a new
architectural layer between the engines and receipts/pricing — both take
the same `ReceiptsStore`/`PricingResolver` the engines already hold, so
there's exactly one code path that resolves a price or queries spend.
A pre-flight block similarly gains a `budget_id` attribute
(`augment_attributes_with_block`, same pattern as the overrun case) so a
real budget block is distinguishable from any other `status: "error"`
receipt — this is what the dashboard's Budgets screen's blocked-request
log filters on, via `GET /v1/local/receipts?status=error`.
`budgets.enforcement.spent_so_far_usd` is public and reused verbatim by
`GET /v1/local/budgets/spend` (below), so the dashboard's burn bar can
never compute a different number than enforcement itself did.

## The local control API boundary

See `docs/adr/0016-local-control-api.md` — including why "local
control API" (MISSION.md's own phrase) is deliberately not called
"control plane": that word is reserved by the ADR just below this one
for a *future hosted*, cross-fleet capability, and this is the opposite
— a second, localhost-only HTTP surface over one process's own SQLite
files. `inferrail serve --app-mode` forces `receipts.sink: sqlite` and
`budgets.enabled: true`, relocates both under `appdata.app_data_dir()`,
generates a mandatory per-install token
(`localapi.token.ensure_local_api_token`), and mounts
`localapi.routes.router` (`/v1/local/receipts`, `/work`, `/budgets`,
`/stream`) alongside the normal `/v1/chat/completions`/`/v1/messages`
routes on the same `FastAPI` app. Every route reuses the same
`ReceiptsStore`/`BudgetStore` instances `create_app` already built for
the engines/enforcer — never a second connection or a second source of
truth for the same file. `/v1/local/stream` is a poll loop over
`ReceiptsStore.query(since=...)` (SQLite has no pub-sub), stopping on
`Request.is_disconnected()`, the same discipline the inference
engines' own streaming already follows.

## The dashboard boundary

See `docs/adr/0017-dashboard-in-app-directory.md`. `app/` is a
self-contained React + Vite + TypeScript SPA, built to `app/dist` — a
static bundle with no server-side rendering and no Node runtime needed
to serve it. `inferrail.dashboard.find_dashboard_dist()` locates a build
(an env override, a bundled `dashboard_static/` inside the installed
package — see `docs/adr/0018-dashboard-wheel-packaging.md` for how that
gets there at wheel-build time, via `hatch_build.py` — or `app/dist`
found by walking up from the source tree, for a git checkout) and
`gateway.app.create_app`
mounts it at `/dashboard` via `StaticFiles(html=True)`, only when
`app_mode=True` and only when a build is actually found — no new route,
no new failure mode for a normal `inferrail serve`. Routing between
dashboard screens is hash-based (`/dashboard/#/live`, ...) specifically
so the server-side mount never needs a SPA catch-all. The dashboard
authenticates to `/v1/local/*` using the same per-install token as any
other local-API caller, but reads it from the page's own URL query
string rather than an `Authorization` header — the CLI prints the token
already embedded in the URL
(`http://host:port/dashboard/?token=...`), and `localapi.routes`'s auth
dependency accepts either form, since browser `EventSource` (used by the
Live Feed screen) cannot set custom headers at all.

**The Recover screen bridges to `inferrail.ap`, a previously separate
module.** `--app-mode` now also constructs an `ap.store.RecoveryStore`
at a fixed app-data path (`INFERRAIL_AP_DB` overrides it), always —
same treatment as receipts/budgets, never conditional on whether the AP
module happens to be in use. `localapi.routes`'s `/ap/pending` and
`/ap/{work_id}/outcome` are thin wrappers: the former calls
`ap.report.build_live_report` (the exact function `inferrail ap report`
and the hosted AP service's own `GET /v1/report` already call) and
filters to `status == "awaiting_human_review"`; the latter calls
`RecoveryStore.record_outcome` directly, the same store-level call
`inferrail ap outcome` makes — no new AP business logic, only a new,
local, single-user way to reach the existing one.

## The usage-ping boundary

See `docs/adr/0019-opt-in-usage-ping.md`. `src/inferrail/usage_ping/` is
deliberately separate from both `telemetry/` and `receipts/` — it does
not reuse `TelemetrySink`/`ReceiptSink` as its own transport, only
`usage_ping.receipt_hook.UsagePingReceiptSink`, which *wraps* whichever
`ReceiptSink` the two inference engines hold (never the raw
`ReceiptsStore` the budget enforcer/local API still use directly, which
need real `.query()`). This is the one deliberate, narrow exception to
this whole repository's "nothing transmits data off the machine by
default" rule, and it's built to be architecturally hard to confuse with
either of those local-only systems.

Off by default (`UsagePingConfig.enabled`), and inert with no
`usage_ping.endpoint` configured — `usage_ping.client.maybe_send_event`
checks `endpoint` first, before touching even the local state file, so
an unconfigured install does zero work per call. A separate, mutable
`usage-ping-state.json` under the OS app-data dir (not the static config
file) owns the on/off toggle at runtime once it exists, the same
"config seeds it, a store owns it after that" pattern
`budgets`/`receipts` already use under `--app-mode`; `endpoint` itself
stays config-only, never dashboard-settable, to close off a redirect-the-
pings-elsewhere abuse surface for no real benefit. Every send happens on
a background thread the caller never waits on, with a short timeout and
every exception swallowed — see the ADR for the full reasoning and the
exact fixed payload shape.

`hosted/usage_ping/` is the proposed, built (but not yet
founder-deployed) reference receiver — its own process, own SQLite
storage, zero dependency on the `inferrail` package, same "own process,
own storage, no shared code path" isolation every other `hosted/`
surface in this repository already follows.

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
