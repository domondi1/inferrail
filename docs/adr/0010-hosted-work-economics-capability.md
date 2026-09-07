# 0010. Add the first hosted capability without touching the data plane

## Status

Accepted

## Context

Inferrail Work Economics is Inferrail's first paid, hosted capability: given
caller-declared economic events for a unit of work, it returns a normalized
cost summary and a commercial receipt, paid for over x402 on a public
testnet. This is exactly the kind of capability ADR 0004 anticipated —
value that comes from Inferrail operating a service, not from a single
local process — and ADR 0004 requires that adding it be additive: it must
not change how the OSS gateway operates without it, and the gateway must
not gain a dependency on it.

## Decision

The capability's code lives in a new top-level `hosted/work_economics/`
directory, not inside `src/inferrail`. It is not part of the `inferrail`
pip package (see `pyproject.toml`'s `[tool.hatch.build.targets.wheel]`
package list, which is unchanged by this addition), is not imported by any
gateway code path, and has its own dependency list
(`hosted/work_economics/requirements.txt`) rather than being added to the
gateway's core dependencies. `inferrail serve` has no code path that calls
out to, depends on, or assumes this service exists or is reachable.

Where this capability's logic overlaps with already-shipped discipline
(Decimal-only money, a structurally payload-free schema, an explicit
unknown-cost count instead of a fabricated total, a caller-declared outcome
echoed back but never interpreted — see ADR 0003 and ADR 0005), it follows
the same rules but does not reuse `inferrail.receipts.InferenceReceipt` or
`inferrail.work.WorkSummary` directly: those types describe receipts
Inferrail's own gateway generated from its own execution, while this
capability is deliberately execution-agnostic — a buyer describes economic
events for work it did anywhere, not necessarily through Inferrail.
Forcing that onto the gateway-specific receipt shape would be a worse fit
than a small, separate, equally-disciplined schema.

## Consequences

- A future outage or removal of this hosted capability cannot break
  `inferrail serve` for any existing self-hosted deployment.
- Future hosted capabilities should follow the same shape: a new directory
  under `hosted/`, its own dependency list, and a decision to reuse or not
  reuse a data-plane schema made on the merits of the fit, not by default
  in either direction.
- `hosted/` is intentionally excluded from `mypy`'s `packages` list and the
  wheel build — it is a deployable service, not part of the installable
  library's public API surface.
