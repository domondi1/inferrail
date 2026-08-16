# Inferrail — Product

> This file is the authoritative source for exact current scope.

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
- `GET /health`
- One provider adapter (`OpenAIProvider`) that speaks the OpenAI
  `/chat/completions` wire format — usable against `api.openai.com` or any
  other endpoint that implements the same shape, via `base_url`
- Static routing: the request's `model` field selects a named route in
  `inferrail.yaml`, which maps to a provider + underlying model
  deterministically. No cost/latency/capability-aware selection.
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
  fields.
- `inferrail report --by <provider|model|route|attribute-name>` —
  aggregates local receipts into a simple table: requests, tokens, total
  known cost, and a separate count of requests with unresolvable pricing
  (never silently folded into the cost total as `$0`).
- YAML config (`inferrail.yaml`) + environment variables for secrets, with
  loud validation errors
- A CLI: `inferrail serve` (`--quickstart` to skip `inferrail.yaml` and use
  in-memory defaults), `inferrail config check`, `inferrail report`,
  `inferrail demo` (offline, zero-key walkthrough of the receipt/report
  pipeline using a fake provider), `inferrail try` (one real request
  through the same `InferenceEngine` `inferrail serve` uses, no config
  file required — needs `OPENAI_API_KEY`)
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

- **Streaming** (`stream: true`) is rejected, not silently ignored
- Multiple choices (`n != 1`) is rejected
- Multi-part / image / audio message content — only plain string content
- Tool calls / function calling
- Cost estimates for anything outside the built-in catalog or an explicit
  operator `pricing:` override — an unrecognized (provider, model) always
  produces `null`, never a guessed cost (see "Cost and receipts" above)
- Time-to-first-token — always `null`; only measurable once streaming
  exists
- Any provider other than an OpenAI-compatible HTTP API
- Intelligent/adaptive routing of any kind
- Budgets, spend limits, or blocking a request based on cost
- Historical price versioning (a receipt embeds the price snapshot used at
  the time, but there is no queryable price-history store)
- A web dashboard — `inferrail report` is a local CLI table, deliberately
- Any hosted/cloud component — see "OSS vs. hosted" below

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
  zero cloud dependency
