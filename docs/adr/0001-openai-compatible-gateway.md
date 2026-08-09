# 0001. Ship an OpenAI-compatible HTTP gateway first

## Status

Accepted

## Context

Inferrail needs a first interface for applications to send inference
requests through. The realistic adoption path for infrastructure like this
is near-zero-friction integration into existing applications, the large
majority of which already speak the OpenAI chat completions request/response
shape (directly, or via an SDK/framework that does).

Alternatives considered:

- A novel Inferrail-specific protocol/SDK: maximal design freedom, but
  requires every adopter to rewrite integration code before getting any
  value — a poor first impression for infrastructure whose whole pitch is
  "minimal or zero meaningful changes."
- gRPC or another binary protocol: better performance characteristics
  eventually, but no existing application code speaks it, and it doesn't
  serve the "change `base_url` and go" story at all.

## Decision

v0.1 exposes `POST /v1/chat/completions` and `GET /health`, matching
OpenAI's request/response shape for the subset of features currently
supported (see `docs/PRODUCT.md` for exact scope — no streaming, no tool
calls, no multi-part content). A non-standard `inferrail` field is added to
successful responses carrying routing/telemetry metadata; standard OpenAI
clients that ignore unknown fields are unaffected.

This is a compatibility surface, not a claim of full API compatibility.
Anything not explicitly listed as supported should be assumed unsupported.

## Consequences

- Existing OpenAI-client-based application code can point at Inferrail by
  changing only a base URL, for the supported request shape.
- We inherit OpenAI's request/response shape as an external constraint on
  the wire format, even though internally requests are normalized into
  Inferrail's own `NormalizedChatRequest`/`NormalizedChatResponse` types
  (see `providers/base.py`) — the wire format and the internal
  provider-facing format are deliberately different types, so wire-format
  compatibility work never leaks into the provider boundary.
- Streaming will eventually be necessary for compatibility claims to hold
  up for real applications; deferring it was a scope decision, not a
  belief that it's unimportant (see `docs/PRODUCT.md`, non-goals).
