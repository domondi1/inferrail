# 0007. Optional model passthrough via `default_provider`

## Status

Accepted

## Context

ADR-0002 made the request's `model` field mean "named Inferrail route,"
looked up in `inferrail.yaml`'s `routes:` section, deliberately not a
provider's model id. That kept routing fully static and auditable, but it
also means every upstream model id an application wants to use has to be
pre-registered as a route before a request naming it will succeed — a
gateway that rejects `model: "gpt-5.6-sol"` because nobody wrote a `routes:`
entry for it yet is not a transparent rail, and updating Inferrail every
time a provider ships a new model name is not a sustainable posture for a
gateway whose entire value proposition is sitting in the request path
without getting in the way.

## Decision

`InferrailConfig` gains an optional `default_provider: str | None` field.
`Router.resolve` tries a named-route lookup first, unchanged from ADR-0002.
Only if that misses, and `default_provider` is set, does it fall back to a
passthrough `RoutingDecision`: the requested provider is
`default_provider`, and `model` is forwarded to it completely unchanged —
not translated, not validated against any known-model list. Named routes
still take priority, so an operator can shadow a real model name with a
route of the same name (e.g. to force nonstandard retry/timeout behavior
for it specifically), and that route wins.

A passthrough decision's `route_name` is the fixed sentinel `"passthrough"`
(`routing.router.PASSTHROUGH_ROUTE_NAME`), not the model name — `inferrail
report --by route` can then still tell "used a configured route" apart
from "passed through," while `--by model` already gives the real
per-upstream-model breakdown within that bucket.

`default_provider` is opt-in and defaults to unset, so every existing
`inferrail.yaml` keeps its current strict behavior with zero change. It is
set by default in the zero-config quickstart path
(`config.quickstart.build_quickstart_config`) — quickstart has exactly one
provider, so there's no ambiguity about where a passthrough request should
go, and "any model name just works" is exactly the zero-friction promise
that path exists to make.

Pricing needed no change: `pricing.resolver.PricingResolver.resolve` was
already a pure function of `(provider_name, model)`, independent of route
name. A passthrough request for a model with no verified price resolves to
`estimated_cost_usd: null` — never a fabricated `$0` — exactly like an
unrecognized model reached through a named route today. Unknown pricing
must never mean unsupported model.

## Consequences

- An application can send any upstream model id Inferrail has never heard
  of and get a working response, provided `default_provider` is set and
  the underlying provider actually supports that model — Inferrail does
  not and cannot validate that a passed-through model id is real.
- `inferrail.yaml` without `default_provider` is unaffected: an unmatched
  `model` still raises `RoutingError` exactly as before ADR-0007.
- A typo in `model` (e.g. `"defualt"`) that would previously fail loudly
  with "no route named" now silently succeeds as a passthrough call to a
  real, differently-named upstream model, if `default_provider` is set.
  This is a deliberate trade-off: the same flexibility that makes an
  unregistered real model work also can't distinguish it from a typo. This
  is judged worth it because a provider's own "model not found" error
  still surfaces to the caller in that case, just one hop later than a
  route-lookup error would have.
- When real routing intelligence is eventually built (ADR-0002's
  deferred `RoutingPolicy`), passthrough is just the current fallback
  behavior of `Router.resolve`'s miss case — nothing about this decision
  blocks or shapes that later work.
