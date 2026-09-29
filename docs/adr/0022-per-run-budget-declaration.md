# 0022. Per-run budgets without pre-registration: a declared or default ceiling per work_id

## Status

Accepted (pending review)

## Context

ADR-0021 made block-mode admission atomic. But protecting a single agent
run still means creating a budget object for its `work_id` first
(`inferrail budget set --scope work_id --scope-value <id> …`), and doing
that again for every new run. Most runs get their id when they start, so
this setup step is the main friction in "give this run a dollar ceiling".

The desired flow:

    mark the work (work_id)
      → declare a budget, or inherit a default
      → no separate setup
      → enforced at admission
      → a final per-run record

## Decision

### How a run gets a budget

A request that carries `X-Inferrail-Attribute-Work-Id: <id>` can be
covered by a `per_work`, block-mode budget for that id in three ways:

1. **Stored:** a budget already exists for `work_id:<id>:per_work`
   (created with the CLI or local API, or created earlier by 2 or 3
   below).
2. **Declared:** the request sends `X-Inferrail-Budget-Usd: <amount>`,
   and no budget exists yet for that id.
3. **Default:** the operator set `budgets.per_work_default_usd`, no
   budget exists yet for that id, and the request doesn't declare one.

With 2 or 3, the budget row is created **inside the same admission
transaction** that reserves the request (ADR-0021's `BEGIN IMMEDIATE`).
So when concurrent first requests for a new id all declare the same
budget, exactly one creates it, and every one of them is admitted
against it. No request for an unseen id can slip past before the budget
exists.

A request with no `work_id` isn't affected. Neither is a `work_id`
request when no budget is stored, declared, or defaulted. Global and
project budgets still apply exactly as before.

### Override hierarchy and immutability

- **The first budget for a work_id wins, and it never changes by header.**
  A later request declaring a *different* amount for the same id is
  refused (`INFERRAIL_E013`, HTTP 400) before any provider call. Silently
  applying either value would hide a bug. An identical declaration
  (retries, or every call of the run sending the same header) is
  accepted.
- **An operator-stored budget beats a declaration.** If one exists and
  the header differs, the request is refused the same way.
- **A declaration can never loosen other budgets.** Global, project and
  other matching budgets are all still enforced; a request must pass
  every one.
- **Operator ceiling:** `budgets.per_work_max_usd`, if set, is the most a
  header may declare. A larger declaration is refused (E013), never
  clamped.
- An operator can turn declarations off with
  `budgets.allow_declared_budgets: false`. The header is then refused
  (E013), never silently ignored.

### Trust boundary and spoofing

- The budget header is read from the HTTP request, i.e. from the
  application code that calls the gateway. The model can't set it: an
  agent's LLM output never becomes an HTTP header unless the application
  deliberately does that.
- Anyone who can reach the gateway can already spend through it. If
  `INFERRAIL_GATEWAY_TOKEN` is set, only authenticated callers get this
  far. A declaration only ever *adds* a limit for an id that has no
  budget yet, so a caller can't use it to raise anyone's limit. The worst
  a hostile caller can do is declare a small budget for a `work_id`
  before its real owner does, which refuses the owner's later calls with
  a clear conflict error. That is a denial of service by someone who is
  already trusted to call the gateway, not overspend. Operators who need
  more should keep declarations off and store budgets themselves.
- Neither the header nor its value is forwarded upstream (headers never
  are; see `gateway/attribution.py`).

### Nested runs, retries, streaming, pricing, failures

- **Sub-agents** that should share the run's ceiling reuse the parent's
  `work_id`. A sub-agent with its own `work_id` gets its own budget,
  and its spend does **not** also count against the parent's. There is
  no budget hierarchy in this version; a `project` budget can bound a
  group of runs.
- **Retries:** a client retry sends the same header, so the same stored
  budget applies. Gateway retries are admitted per attempt (ADR-0021).
- **Streaming, cancellation, timeouts, unknown pricing:** unchanged from
  ADR-0021. Settlement releases or holds; an unpriced model is refused
  under the declared budget (E012).
- **Invalid declarations** are refused with E013 before any provider
  call: a value that isn't a positive decimal, a header sent without a
  `work_id`, a value above the operator ceiling, a conflict, or
  declarations turned off.
- **Storage growth:** each declared or defaulted run leaves one small
  budget row. `inferrail budget rm` removes one; bulk cleanup is future
  work.

### Running in front of another gateway (coexistence)

A team can keep its current gateway (LiteLLM, otari, OpenRouter, …) and
add Inferrail in front of it just for per-run control:

    framework → Inferrail → existing gateway → provider

Two opt-in settings on an `openai_compatible` provider remove the
friction found in testing:

- `price_as: openai` (or `anthropic`) is the operator asserting that this
  upstream bills at that vendor's list prices. The built-in catalog then
  applies, also for vendor-prefixed ids (`openai/gpt-4o-mini`,
  `openai:gpt-4o-mini`). The price's recorded source says it was
  applied through `price_as`, so it's never presented as verified.
  Explicit `pricing:` overrides still win. Without either, a block budget
  refuses the model (E012), as before.
- `request_stream_usage: true` asks the upstream for the final usage
  chunk on streams, as Inferrail already does for verified OpenAI.
  Without it, a stream whose client didn't request usage stays unpriced
  and its reservation is held.

Each gateway enforces only its own budgets, so nothing is counted twice.
A downstream gateway's refusal (for example a 429 from its own budget) is
an HTTP error, so Inferrail releases the reservation.

## Consequences

- Protecting one run becomes one extra header, or zero extra headers with
  a configured default. There is no pre-registration step.
- A declared budget behaves exactly like a stored one after creation
  (visible in `inferrail budget list`, same reservation semantics).
- Declarations need `budgets.enabled: true` (a sqlite receipts store),
  as all budgets do.
