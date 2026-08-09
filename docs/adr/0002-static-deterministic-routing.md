# 0002. Static, deterministic routing for v0.1; route name carried in `model`

## Status

Accepted

## Context

Inferrail's long-term thesis involves increasingly sophisticated routing
(cost/latency-aware, capability-aware, reliability-aware). Building that
now would mean making optimization decisions before any trustworthy
telemetry exists to base them on — the opposite of the order of operations
this project is committed to (observable before optimizable).

We also needed a concrete way for a client to express "which route do I
want" using an unmodified OpenAI-compatible request, which only has a
`model` field to work with.

## Decision

v0.1 routing is a static, explicit lookup: `inferrail.yaml` defines named
`routes`, each mapping directly to one `provider` + one underlying `model`.
A client selects a route by putting its name in the request's `model`
field (e.g. `{"model": "default", ...}`). `Router.resolve` is a pure
function of this config — no runtime signals (cost, latency, historical
reliability) are considered.

`RoutingContext` and `RoutingDecision` are still introduced as distinct
types now, even though `RoutingContext` currently carries only
`requested_route`, so that richer signals can be added to `RoutingContext`
later without changing what `InferenceEngine` consumes from
`RoutingDecision`.

## Consequences

- Operators must know a route's name to reach it — there is no "just give
  me your best model for this task" mode yet. This is intentional: it
  keeps behavior fully predictable and auditable while the project earns
  trust.
- Overloading the OpenAI `model` field to mean "Inferrail route" rather
  than "provider model id" is a deliberate compatibility trade-off: it
  lets existing OpenAI client code work unmodified (just point `base_url`
  at Inferrail and set `model` to a configured route name), at the cost of
  the field's value not matching an actual upstream model id. This is
  documented in `docs/PRODUCT.md` and the example config, not left
  implicit.
- When real routing intelligence is built, it should replace the body of
  `Router.resolve` (or sit behind a `RoutingPolicy` abstraction with
  multiple strategies) without requiring changes to `InferenceEngine`,
  `providers/*`, or the HTTP layer.
