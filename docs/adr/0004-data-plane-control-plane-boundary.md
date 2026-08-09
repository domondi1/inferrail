# 0004. Keep the data plane runnable with zero control-plane dependency

## Status

Accepted

## Context

Inferrail's intended business model is open-core: an open-source data
plane driving adoption, with a future hosted control plane providing
capabilities that naturally compound with scale and history (fleet
observability, cross-deployment analytics, policy management). Getting
this boundary wrong early — by building cloud hooks into the hot path, or
by crippling the OSS version to manufacture demand for a cloud product —
would undermine both the trust the OSS project needs to earn and the
architecture's ability to actually support a hosted product later without
a rewrite.

## Decision

The repository currently contains **only** the data plane. No code path
calls out to, depends on, or assumes the existence of an
Inferrail-operated service. Nothing here is a stub or fake implementation
of a future cloud feature — where a future extension point is anticipated
(e.g. `TelemetrySink`), only an *interface* exists, with concrete
implementations limited to what's genuinely useful locally today
(console, JSONL, null).

The dividing line used to decide "does this belong in the OSS data plane
or a future hosted control plane" is whether a capability's value comes
from a single request/process (data plane) or from aggregating many
requests/processes over time or across a fleet (naturally hosted):
provider execution, retries, and per-request telemetry are data-plane;
cross-deployment analytics, historical performance comparison, and policy
management across many deployments are control-plane.

## Consequences

- A future Inferrail Cloud outage cannot prevent an already-configured
  local deployment from serving inference — there is nothing to be
  outaged, since no cloud dependency exists yet.
- Every future "hosted" feature must be justified by this test, not by
  what's commercially convenient to withhold from OSS. Capabilities that
  are naturally single-process (e.g. "route based on this request's
  attributes") stay in the data plane even after a control plane exists.
- Adding the first real hosted integration later (e.g. an opt-in cloud
  telemetry sink) should be additive: a new `TelemetrySink` implementation
  and explicit config to enable it, not a change to how the data plane
  operates without it.
