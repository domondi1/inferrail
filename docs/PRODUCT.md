# Inferrail — Product

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
happened.

## Who it's for

Developers and small teams running LLM-backed applications who want:

- a single place to point an OpenAI-compatible client, instead of
  provider-specific SDK code sprinkled through the app
- to actually know, per request, what provider/model served it, how long it
  took, and whether it failed — without adding a hosted observability
  vendor
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
- YAML config (`inferrail.yaml`) + environment variables for secrets, with
  loud validation errors
- A CLI: `inferrail serve`, `inferrail config check`
- Optional shared-secret gateway auth: if `INFERRAIL_GATEWAY_TOKEN` is set,
  `/v1/chat/completions` requires a matching `Authorization: Bearer`
  header. Unset by default (localhost-dev mode) — see README's "Security"
  section. Not a user/auth system; a single shared secret.

### Explicit non-goals / not yet supported

Not a hidden limitation — these are the honest edges of v0.1:

- **Streaming** (`stream: true`) is rejected, not silently ignored
- Multiple choices (`n != 1`) is rejected
- Multi-part / image / audio message content — only plain string content
- Tool calls / function calling
- Cost estimates — no verified pricing table yet, so `estimated_cost_usd`
  is always `null` rather than a guess
- Time-to-first-token — always `null`; only measurable once streaming
  exists
- Any provider other than an OpenAI-compatible HTTP API
- Intelligent/adaptive routing of any kind
- Any hosted/cloud component — see "OSS vs. hosted" below

## Long-term direction

The progression Inferrail is built to support, in order, is:

observable → controllable → measurable → comparable → optimizable →
increasingly intelligent

Each step needs the one before it to be trustworthy first. v0.1 delivers
the first step (observable: a real telemetry record for every request)
and the scaffolding for the second (controllable: explicit routing
config, provider abstraction). Later phases — comparing providers/models
on cost and quality, recommending routing policies, fleet-wide analytics —
depend on operational history accumulating across many requests, which is
naturally a *hosted* capability once a user wants it to span more than one
machine or process. See `ARCHITECTURE.md` for how the OSS data plane and a
future hosted control plane are meant to stay decoupled.

## Non-goals (for the project generally, not just this session)

Inferrail is not attempting to become:

- A LangChain/LiteLLM-style all-in-one framework
- A prompt-management, RAG, or agent framework
- A vector database
- An evaluation platform (though evaluation could plausibly build on top of
  its telemetry later)
- A hosted-only product — the data plane must remain genuinely useful with
  zero cloud dependency
