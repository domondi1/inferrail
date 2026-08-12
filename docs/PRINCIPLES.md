# Inferrail — Engineering Principles

These are the durable engineering principles behind Inferrail. They're
meant to survive individual feature decisions — if a change conflicts
with one of these, that's a signal to stop and reconsider, not to route
around it.

## Local-first / OSS data plane independence

The gateway must work correctly with zero dependency on any
Inferrail-operated service. No code path calls out to, depends on, or
assumes a remote Inferrail service exists. Everything in this repository
is designed to keep running indefinitely with nothing but the machine
it's deployed on. See
[docs/adr/0004-data-plane-control-plane-boundary.md](adr/0004-data-plane-control-plane-boundary.md).

## Payload privacy by default

No prompt or response content is persisted or transmitted by default.
This is a schema-level guarantee — there is no field to hold that content
— not a runtime flag that could be misconfigured on, and it's enforced by
a test. See
[docs/adr/0003-no-payload-persistence-by-default.md](adr/0003-no-payload-persistence-by-default.md).

## No secret telemetry

Nothing leaves the machine Inferrail runs on unless an operator has
explicitly configured a sink that does that, and any such sink must be
documented as such. No hidden network calls.

## No fabricated metrics

Values Inferrail cannot currently measure trustworthily (cost,
time-to-first-token, etc.) are `null`, never estimated or guessed.
Unknown stays unknown until there's a verified way to know it.

## Deterministic, testable behavior

Routing and execution are pure, inspectable functions of configuration
wherever possible. The core engine (`InferenceEngine`, `Router`,
provider adapters) is independent of the web framework so it can be
tested directly, without spinning up an HTTP server.

## Small, simple hot path

The request-execution path is kept as small and easy to reason about as
possible. Avoid unnecessary dependencies and avoid speculative
complexity — no architecture theater, no fake scale, no infrastructure
for a feature that doesn't exist yet.

## Explicit supported-vs-roadmap distinction

What Inferrail actually does today and what it doesn't do yet are always
stated explicitly and kept in sync with the code — see "Current scope"
and "Explicit non-goals" in [docs/PRODUCT.md](PRODUCT.md). A missing
feature is a documented non-goal, not a silent gap.

## Provider neutrality

The provider boundary is a protocol, not a hard-coded integration. Any
endpoint that speaks a supported wire format is usable via configuration
alone; a genuinely different protocol gets its own adapter behind the
same interface. See
[docs/ARCHITECTURE.md](ARCHITECTURE.md#the-provider-boundary).

## Transparent failure semantics

Provider and routing failures are normalized into a small, explicit
exception hierarchy and mapped to well-defined HTTP responses — never
swallowed, never surfaced as a raw upstream stack trace.

## Measure before optimizing

No performance work happens without a benchmark to justify it, and no
performance claims are made in this codebase without one backing them.
