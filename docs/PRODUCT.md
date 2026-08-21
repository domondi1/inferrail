# Inferrail — Product

> This file is the authoritative source for exact current scope.

**Developer Preview · v0.1.0.** The scope below is fully implemented and
tested, but nothing is stable yet — CLI flags, `inferrail.yaml`'s shape,
and receipt/telemetry JSON fields may change without notice before v1.0.
See README.md's status note.

## What it is

Inferrail is an open inference control plane: infrastructure that sits
between an application and the model providers it calls, so operational
decisions (which provider, which model, retry/fallback behavior, what
happened and why) live in configuration and telemetry rather than scattered
across application code.

v0.1 is the first, narrow slice of that: an OpenAI-compatible HTTP gateway
that takes a chat completion request, routes it to one explicitly
configured provider/model via a static policy, executes it, and returns a
compatible response plus a structured local telemetry record of what
happened — plus, as of this slice, a privacy-preserving economic receipt:
what that execution cost, computed from measured usage and verified
pricing, tied to business context the caller attaches. The long-term
thesis this is the first step of: measure → attribute → connect to
outcome → govern → optimize.

## Who it's for

Developers and small teams running LLM-backed applications who want:

- a single place to point an OpenAI-compatible client, instead of
  provider-specific SDK code sprinkled through the app
- to actually know, per request, what provider/model served it, how long it
  took, and whether it failed — without adding a hosted observability
  vendor
- to know what each customer, workflow, or feature is actually costing
  them in model spend, without storing their users' prompts to figure
  that out
- a foundation they can run entirely on their own machine or
  infrastructure, with no dependency on an Inferrail-operated service

## The problem being solved right now

Applications that call LLM providers directly hard-code operational
decisions (which provider, which model, how to handle a 429) into
business logic. Inferrail v0.1 moves that decision to a deterministic,
inspectable config file and gives you a telemetry record for every request
— the prerequisite for anything smarter later (see "Long-term direction").

## Current scope (v0.1) — what works today

- `POST /v1/chat/completions` — OpenAI-compatible request/response shape
  for single-turn or multi-turn text chat (see limits below)
- Real SSE streaming (`stream: true`): upstream bytes are proxied
  byte-for-byte as they arrive, never buffered and re-chunked. Retries
  only ever happen before the first byte reaches the client — once a
  stream has yielded anything, a later provider failure or client
  disconnect ends that stream and is recorded as `status: "partial"` on
  its `InferenceEvent`/`InferenceReceipt`, never silently retried (which
  would replay already-observed output) and never given a fabricated
  cost: a partial record only carries whatever usage was actually
  measured before the interruption — `null` if none was.
- Tool/function calling: `tools`, `tool_choice`, and `parallel_tool_calls`
  are accepted and passed through to the provider unmodified, including
  parallel tool calls and streamed tool-call deltas. `role: "tool"`
  messages (tool results) are accepted. Inferrail transports tool-call
  semantics — it never executes a tool itself, never parses or
  re-serializes a tool call's `arguments` string (kept byte-exact end to
  end), and never reorders or renames a call.
- `GET /health`
- One provider adapter (`OpenAIProvider`) that speaks the OpenAI
  `/chat/completions` wire format — usable against `api.openai.com` or any
  other endpoint that implements the same shape, via `base_url`
- Static routing: the request's `model` field selects a named route in
  `inferrail.yaml`, which maps to a provider + underlying model
  deterministically. No cost/latency/capability-aware selection.
- Optional model passthrough: if `default_provider` is set in
  `inferrail.yaml`, a `model` that matches no named route is forwarded to
  that provider unchanged instead of being rejected — so an application
  can use any upstream model id (including ones released after this
  version of Inferrail) without a route being pre-registered for it. Named
  routes still take priority. Off by default for an explicit config; on by
  default for the zero-config quickstart path. See
  `docs/adr/0007-model-passthrough-routing.md`.
- Fixed-count retry with linear backoff for transient provider errors
  (timeouts, rate limits, 5xx), configurable per route
- A structured `InferenceEvent` emitted for every request (success or
  failure): request id, route, provider, model, status, latency, token
  counts when available, retry count. No prompt or response content by
  default.
- Two local telemetry sinks: console (structured log line) and a local
  JSONL file. Nothing leaves the machine.
- A payload-free `InferenceReceipt` emitted for every request (success or
  failure): provider, model, token counts, a `Decimal` cost computed from
  measured usage and a verified price, the price's provenance (source +
  verified date), caller-supplied business attribution, latency, retries,
  status. Local JSONL sink by default (`receipts.path`, default
  `./inferrail-receipts.jsonl`). See "Cost and receipts" below.
- Caller-supplied business attribution: `X-Inferrail-Attribute-<Name>` HTTP
  headers (e.g. `X-Inferrail-Attribute-Customer: acme`) are collected into
  a generic `dict[str, str]` and persisted on the receipt. Never forwarded
  to the upstream provider. Generic by design — no fixed vertical-specific
  fields, which already covers correlating receipts across a multi-turn
  agent loop's several inference calls: e.g.
  `X-Inferrail-Attribute-Run: run_123` or `-Agent`/`-Trace`/`-Workflow`
  work today with no new primitive needed.
- `inferrail report --by <provider|model|route|attribute-name>` —
  aggregates local receipts into a simple table: requests, tokens, total
  known cost, and a separate count of requests with unresolvable pricing
  (never silently folded into the cost total as `$0`).
- `inferrail transaction <task-id> [--attribute-name NAME] [--json]` —
  groups every receipt sharing one attribution-attribute value (default
  attribute: `task_id`) into a single `TaskTransaction`: the list of
  contributing receipts, a known-cost total, a separate unknown-cost-event
  count, and an aggregate status (`success` only if every event succeeded,
  `error` only if every event failed, `partial` otherwise). A read-side
  view over existing receipts — attach the same
  `X-Inferrail-Attribute-Task-Id: <id>` header to every request belonging
  to one task, no other setup required. See
  `docs/adr/0008-task-transactions.md`. v0.1 of this primitive: one event
  type (`inference`) — non-LLM resource types are not yet supported (see
  "Explicit non-goals" below).
- `import inferrail; inferrail.track_task(task_id="...")` — an
  **experimental** Python helper that attaches `X-Inferrail-Attribute-Task-Id`
  to every outgoing request ambiently for the duration of a `with` block or
  decorated function, via a `contextvars.ContextVar` plus an `httpx` event
  hook (`inferrail.attributed_http_client()`/`attributed_async_http_client()`,
  handed to any httpx-based SDK client's `http_client=` argument — verified
  against the real `openai` SDK and LangChain's `ChatOpenAI`). Removes the
  need to thread `task_id` through nested function signatures by hand. No
  gateway/schema change; purely a client-side convenience over the header
  mechanism above. `task_id` only, no public API stability commitment yet.
  See `docs/adr/0009-ambient-task-tracking.md`.
- YAML config (`inferrail.yaml`) + environment variables for secrets, with
  loud validation errors
- A CLI: `inferrail serve` (`--quickstart` to skip `inferrail.yaml` and use
  in-memory defaults), `inferrail config check`, `inferrail report`,
  `inferrail transaction`, `inferrail demo` (offline, zero-key walkthrough
  of the receipt/report pipeline using a fake provider), `inferrail try`
  (one real request through the same `InferenceEngine` `inferrail serve`
  uses, no config file required — needs `OPENAI_API_KEY`)
- A configless quickstart path: `inferrail try` and `inferrail serve
  --quickstart` both build the same `InferrailConfig` type `inferrail.yaml`
  loads into, just from an in-memory default (OpenAI, `gpt-4o-mini`,
  receipts at `./inferrail-receipts.jsonl`) instead of a file — not a
  second config system, and it never silently writes a config file to disk
- Optional shared-secret gateway auth: if `INFERRAIL_GATEWAY_TOKEN` is set,
  `/v1/chat/completions` requires a matching `Authorization: Bearer`
  header. Unset by default (localhost-dev mode) — see README's "Security"
  section. Not a user/auth system; a single shared secret.

### Cost and receipts

- Pricing comes from a small built-in catalog of prices independently
  verified against OpenAI's own published pricing page (currently
  `gpt-4o-mini`, `gpt-4o`), plus an optional `pricing:` section in
  `inferrail.yaml` for operator-declared overrides or additions — both
  forms require an explicit `source` and `verified_date`, so a price's
  provenance is never lost.
- The built-in catalog only applies to a provider configured as
  `type: openai` with the default `base_url` (i.e. verifiably OpenAI's own
  API) — never guessed onto an `openai_compatible` endpoint that merely
  shares the wire format, since that could be serving a completely
  different, differently-priced model under a colliding name. See
  `docs/adr/0005-privacy-preserving-economic-receipts.md`.
- Unresolvable pricing (unknown model, or an `openai_compatible` provider
  with no override configured) leaves `pricing`/`estimated_cost_usd`
  explicitly `null` on the receipt — never a fabricated `$0`.
- All money arithmetic uses `Decimal`, never `float`.

### Explicit non-goals / not yet supported

Not a hidden limitation — these are the honest edges of v0.1:

- Multiple choices (`n != 1`) is rejected
- Multi-part / image / audio message content — only plain string content
- Cost estimates for anything outside the built-in catalog or an explicit
  operator `pricing:` override — an unrecognized (provider, model) always
  produces `null`, never a guessed cost (see "Cost and receipts" above)
- Time-to-first-token — always `null`. Streaming now exists, so this is
  measurable in principle (the first upstream chunk's arrival is already
  observed internally), but Inferrail doesn't populate it yet — a
  natural, low-risk follow-up once there's a reason to, not silently
  guessed in the meantime
- Any provider other than an OpenAI-compatible HTTP API
- Intelligent/adaptive routing of any kind
- Budgets, spend limits, or blocking a request based on cost
- Historical price versioning (a receipt embeds the price snapshot used at
  the time, but there is no queryable price-history store)
- A web dashboard — `inferrail report` is a local CLI table, deliberately
- Any hosted/cloud component — see "OSS vs. hosted" below
- Non-LLM economic events (browser, search, compute/sandbox, MCP tool
  cost) in `TaskTransaction` — its only event type today is `inference`;
  see `docs/adr/0008-task-transactions.md`
- Outcome or business-value linkage (success/failure signal, revenue,
  margin) on a `TaskTransaction` — it aggregates cost only
- Budget enforcement or any policy decision at the transaction level —
  `inferrail transaction` only reports, it never blocks a request

## Verifying privacy claims yourself

README.md's "Privacy" section shows a quick `inferrail try`-based check.
The same claim — that Inferrail never persists prompt or response content
— can be checked against a running gateway:

In `inferrail.yaml`, set:

```yaml
telemetry:
  sink: jsonl
  path: inferrail-telemetry.jsonl
```

Restart `inferrail serve`, then:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "default", "messages": [{"role": "user", "content": "MARKER-1234-do-not-persist-me"}]}'

grep -c "MARKER-1234" inferrail-telemetry.jsonl   # 0, every time
cat inferrail-telemetry.jsonl                     # latency, tokens, status — no message content

grep -c "MARKER-1234" inferrail-receipts.jsonl    # 0, every time (receipts are on by default)
cat inferrail-receipts.jsonl                      # tokens, cost, pricing — no message content
```

This checks only what Inferrail itself writes to disk; your provider
still receives the real prompt either way — Inferrail is a pass-through
gateway to it, not a privacy boundary against it.

## Long-term direction

The progression Inferrail is built to support, in order, is:

observable → controllable → measurable → comparable → optimizable →
increasingly intelligent

Each step needs the one before it to be trustworthy first. v0.1 delivered
the first step (observable: a real telemetry record for every request)
and the scaffolding for the second (controllable: explicit routing
config, provider abstraction). This slice takes the first real step into
measurable: a per-request economic receipt (verified pricing, `Decimal`
cost, business attribution) and a local report to read it back — still a
single-process, single-machine capability, not fleet-wide history. Later
phases — comparing providers/models on cost and quality across many
requests, recommending routing policies, fleet-wide analytics — depend on
operational history accumulating across many requests/deployments, which
is naturally a *hosted* capability once a user wants it to span more than
one machine or process. See `ARCHITECTURE.md` for how the OSS data plane
and a future hosted control plane are meant to stay decoupled.

## Non-goals (for the project generally, not just this session)

Inferrail is not attempting to become:

- A LangChain/LiteLLM-style all-in-one framework
- A prompt-management, RAG, or agent framework
- A vector database
- An evaluation platform (though evaluation could plausibly build on top of
  its telemetry later)
- A hosted-only product — the data plane must remain genuinely useful with
  no Inferrail-hosted service required
