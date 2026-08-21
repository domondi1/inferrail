# 0008. `TaskTransaction`: a read-side, task-level view over existing receipts

## Status

Accepted

## Context

`InferenceReceipt` (ADR-0005) answers "what did this one execution cost."
An autonomous agent task is rarely one execution — it's often several LLM
calls (and, in the future, other resource types) working toward one goal.
Nothing in Inferrail today can answer "what did *this task* cost" without
a caller manually summing receipts themselves.

A private evaluation (not duplicated here — see the project's internal
strategy repository for the full competitive/technical analysis) checked
eight adjacent products and open standards (LLM gateways, agent-cost
libraries, an observability platform, OpenTelemetry's GenAI semantic
conventions, and the FinOps FOCUS spec) for this specific capability:
provider-neutral, payload-free, task-level cost correlation. None provide
it — the sharpest, most consistent gap across everything checked is
task-level correlation across multiple calls, which doesn't exist even in
the standards positioned to define it. That finding is what this feature
tests.

Two questions had to be answered before writing code:

1. Does this require a change to the gateway's request path, or can it be
   built entirely as a read-side view over what already gets recorded?
2. What's the smallest schema that groups receipts without duplicating
   `InferenceReceipt` or creating a second place a payload field could
   ever be added?

## Decision

**Zero gateway/request-path changes.** A caller that wants several
requests grouped into one task already has everything needed: attach the
same `X-Inferrail-Attribute-<Name>` header (e.g.
`X-Inferrail-Attribute-Task-Id: bug_9281`) to every request belonging to
that task — attribution already persists this verbatim on each receipt
(ADR-0005). `inferrail transaction <task_id>` (`cli/transaction.py`) reads
the receipts JSONL file, and `transactions.builder.build_transaction`
filters to the receipts whose chosen attribute (default: `task_id`)
matches, then aggregates them into one `TaskTransaction`.
`InferenceEngine`, `InferenceEvent`, and `InferenceReceipt` are completely
unchanged — this is new code layered on top, with the same "hot path stays
small" property every previous receipt/attribution feature preserved.

**`TaskTransaction` references child events, it never inlines them.**
`EconomicEventRef` carries only `event_type`, `event_id`, `cost_usd`, and
`status` — a pointer to an existing `InferenceReceipt`, not a copy of it.
This keeps `TaskTransaction` from ever becoming a second schema a payload
field could sneak into, and keeps it stable as a schema even if
`InferenceReceipt` itself grows fields later. `event_type` is
`Literal["inference"]` — the only value that exists today — deliberately
not a plain `str`, so a second resource type (search, browser, compute,
...) is a visible, additive schema change whenever it's actually built,
not something that could silently start accepting arbitrary strings today.
**Building that second resource type is explicitly out of scope for this
change** — see the project's internal roadmap for why it's evidence-gated
separately.

**Cost/status aggregation mirrors `cli.report.aggregate` exactly**, so the
two commands never disagree about what counts as an unresolved pricing
gap: only a `status: "success"` event with `cost_usd: null` counts toward
`unknown_cost_event_count` — a failed or partial event legitimately has no
cost to know, and must never be counted as a pricing gap it isn't. The
transaction's overall `status` is `"success"` only if every event
succeeded, `"error"` only if every event failed, and `"partial"`
otherwise (any mix, or any event itself `"partial"`) — deterministic and
tested, not inferred.

**No match returns `None`, not an empty transaction.** "No receipt has
this task id" (a lookup miss, e.g. a typo) is a different, distinguishable
outcome from "this task's events all have unresolvable pricing" (a real
transaction with `known_total_cost_usd == 0` and a nonzero
`unknown_cost_event_count`) — collapsing them would either hide a typo or
misrepresent a real zero-cost pricing gap.

## Consequences

- New package `transactions/` (`schema.py`, `builder.py`), new CLI command
  `inferrail transaction <task_id> [--attribute-name NAME] [--json]`,
  parallel in structure to `report`/`cli/report.py`. `_resolve_receipts_path`
  in `cli/main.py` is now shared between `report` and `transaction` rather
  than duplicated.
- Every receipts JSONL file already written by existing deployments works
  immediately — no migration, no new field on `InferenceReceipt`. A
  transaction only exists for task ids a caller already tagged via
  attribution; nothing retroactively groups untagged historical receipts.
- This is v0.1 of the primitive: one event type (`inference`), no
  aggregate business-value/outcome fields, no policy evaluation. Extending
  it to non-LLM resource types, outcome linkage, or budget enforcement are
  each their own, separately-justified future changes — not implied by
  this one.
