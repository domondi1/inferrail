# 0015. Budget enforcement: scope, pre-flight estimate, and post-flight overrun

## Status

Accepted

## Context

`MISSION.md`'s v0.3.0 calls for "budgets with real enforcement: budget
entities scoped to global/project/work_id with window (per-work/daily/
monthly) and mode (warn/block); pre-flight catalog-based upper-bound
estimate + spent-so-far check in the gateway; block responses are
machine-readable; post-flight reconciliation records honest
`budget_overrun_usd`", plus `inferrail budget set|list|rm`. The
project's own non-negotiable is explicit: "Enforcement is real: a
budget cap blocks or queues at the proxy, not a warning after the
bill" (`MISSION.md`).

Two things this feature needs that nothing before it did:

1. **A queryable view of spend.** "Spent so far" for a given scope and
   window means summing `estimated_cost_usd` across matching receipts.
   `JSONLReceiptSink` (the default) has no query surface at all — every
   reader re-scans the whole file. `ReceiptsStore.query()`
   (docs/adr/0013) already exists for exactly this kind of lookup.
2. **An upper-bound cost estimate before any tokens exist.** Inferrail
   has no tokenizer dependency anywhere (a deliberate choice — see
   docs/PRODUCT.md's non-goals), so a pre-flight check cannot know the
   real prompt token count. It can, however, compute a number that is
   *never smaller* than the real cost, which is all a pre-flight block
   decision actually needs.

## Decision

**Budgets require `receipts.sink: sqlite`.** `InferrailConfig` gains
`budgets: BudgetsConfig` (`enabled: bool = False`, `path: str`). A
model-level validator refuses to load a config with `budgets.enabled:
true` and `receipts.sink` other than `sqlite` — the same fail-fast
pattern `TelemetryConfig` already uses for its own jsonl-path
requirement. This is a real constraint, not a formality: there is no
other efficient way to answer "how much has this project/work_id spent
so far" without re-deriving the same indexed access `ReceiptsStore`
already provides.

**`enabled` defaults to `False`, and nothing budget-related touches
disk unless it's `True`.** `create_app` only constructs a `BudgetStore`
(and wires a `BudgetEnforcer` into both `InferenceEngine` and
`AnthropicInferenceEngine`) when `config.budgets.enabled`. Every other
config — including every existing test's `base_config` — never creates
a stray `inferrail-budgets.db` file as a side effect of building the
app.

**A `Budget` is scope + window + mode + limit, with a deterministic
id.** `scope` is `global` / `project` / `work_id`; `scope_value` is
`None` for `global` and required otherwise; `window` is `per_work` /
`daily` / `monthly`, where `per_work` only makes sense paired with
`scope: work_id` (a global or project budget with no time boundary
would never reset — rejected at the schema level). `budget_id` is
computed as `f"{scope}:{scope_value or '_'}:{window}"`
(`schema.new_budget_id`), so `inferrail budget set` for the same
(scope, scope_value, window) triple is naturally an upsert — never a
silently-accumulating duplicate — and `list`/`rm` need no separate
id-generation step.

**A separate SQLite store from receipts** (`budgets.store.BudgetStore`,
same connection discipline as `ReceiptsStore` — one connection per
call, WAL, `BEGIN IMMEDIATE`), because budgets and receipts have
completely different write-volume/locking profiles (a handful of rare
writes vs. one write per request) and no benefit from sharing a file.
`inferrail budget set|list|rm` operate on this store directly via
`--db`, independent of whether `budgets.enabled` is set — an operator
can author budgets before turning enforcement on.

**Pre-flight estimate is a deliberate, documented overestimate, not a
real token count.** `approx_char_count` sums string lengths anywhere in
the request's messages/system content; dividing by 3 (not the ~4
real tokenizers average) turns that into a prompt-token count that is
never smaller than reality. Completion tokens use the request's own
`max_tokens` when given — Anthropic's Messages API always requires it,
so this is exact on that path — or a documented constant,
`DEFAULT_MAX_COMPLETION_TOKENS_ESTIMATE = 4096`, on the OpenAI path when
the caller omitted it. An unrecognized (provider, model) pair — no
verified price — makes the estimate `None`, and a budget with no
estimate to project is simply skipped, never treated as "$0 spent" —
the same honesty rule `receipts.builder.build_receipt` already applies.

**"Block" raises before the provider is contacted; "warn" never
raises.** `BudgetEnforcer.check` (called from both engines right after
routing resolves the provider/model, before any provider call) computes
`spent_so_far + estimate` per matching budget; if that exceeds a
`block`-mode budget's `limit_usd`, it raises `BudgetExceededError` — a
new `InferrailError` subclass, mapped to HTTP 402, with a registered
code (`INFERRAIL_E010`). A `warn`-mode budget that would be exceeded
only logs; it never blocks and never raises. `gateway/app.py`'s
`ErrorDetail` gained an optional `details: dict[str, str]` field so a
block response carries `budget_id`/`scope`/`window`/`mode`/`limit_usd`/
`spent_so_far_usd`/`estimated_request_usd`/`projected_total_usd`
alongside the human-readable `message` — MISSION.md's "block responses
are machine-readable" criterion.

**A block is still recorded — never a silent rejection.** MISSION.md's
acceptance criterion is "blocked before the provider is called *and the
block is visible in the store*". Both engines' `_check_budgets` catches
`BudgetExceededError` and calls the same `_emit_failure` path any other
pre-execution rejection (a routing error, an unsupported feature) uses,
so a blocked request still produces a `status: "error"` receipt (no
tokens, no cost — honest, matching every other failure) and a telemetry
event with a new `error_category: "budget_exceeded"`.

**Post-flight overrun is attached to the receipt's own `attributes`,
not a new schema field.** Once actual usage is known (success or a
partial stream), `BudgetEnforcer.augment_overrun` recomputes
spend-so-far *before* this request plus its *actual* cost; if that
exceeds any matching budget's limit, it adds a `budget_overrun_usd`
attribute (the worst overrun across all matching budgets) before the
receipt is emitted. `InferenceReceipt.attributes` is already the
open-ended, generic mechanism this codebase uses for exactly this kind
of derived business fact (see `gateway/attribution.py`) — a new typed
field would duplicate that mechanism for no benefit. This is the only
place an overrun is ever recorded: never fabricated at pre-flight time
(the estimate is deliberately conservative, and a request a `block`
budget let through by definition didn't project one).

**Budgets are shared, as-is, between `/v1/chat/completions` and
`/v1/messages`.** Exactly like routing, pricing, receipts, and telemetry
already are (docs/adr/0014) — one `BudgetEnforcer`, wired into both
engines by `create_app`, so a budget scoped to a project applies
regardless of which wire format a request arrives on.

## Consequences

- An operator who never sets `budgets.enabled: true` sees zero behavior
  change — no new file on disk, no new check on the request path.
- Turning enforcement on requires `receipts.sink: sqlite` — an operator
  currently on the default `jsonl` sink must switch (see
  docs/adr/0013's own migration story, `inferrail receipts import`) before
  they can use budgets at all. This is a real, documented constraint,
  not a bug.
- The pre-flight estimate can reject a request whose *actual* cost
  would have stayed under the limit (it's a ceiling, not a prediction).
  This is the correct trade-off for a hard cap: MISSION.md requires the
  cap to block *before* spending, which is only possible against an
  upper bound.
- Known limitation: `DEFAULT_MAX_COMPLETION_TOKENS_ESTIMATE` is a fixed
  constant, not a per-model real output ceiling (the pricing catalog
  doesn't currently carry one). A future session could add a verified
  `max_output_tokens` field to `PriceEntry` and use it here instead —
  tracked as a possible follow-up, not required for v0.3.0's own
  acceptance criteria.
- The local control API's budgets CRUD (v0.3.0 unit 4) has
  `BudgetStore` to build on directly instead of re-deriving its own
  persistence.
