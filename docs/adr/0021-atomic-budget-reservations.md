# 0021. Atomic budget reservations, streamed-call accounting, and an explicit request-field policy

## Status

Accepted (pending review)

## Context

ADR-0015's block-mode budgets check `spent_so_far + estimate <= limit`
and then call the provider. Spend is only recorded when the receipt is
written, after the provider call. That is a check-then-act race: when
several requests share a budget scope (parallel tool calls, sub-agents,
concurrent runs of one job under one `work_id`), each can pass the check
before any of them records spend, and together they spend past the
limit. The same gap applies across gateway processes sharing one budgets
database.

Three related gaps make a per-run dollar ceiling for an agent run less
trustworthy than it should be:

1. **Concurrency** — the race above.
2. **Streamed calls with no reported usage** — an OpenAI-shaped stream
   only carries usage if `stream_options.include_usage` is set. Without
   it, the receipt's cost is `null` (honest), but that spend then counts
   as $0 against the budget, so a block budget quietly stops limiting
   anything.
3. **Request fields** — `ChatCompletionRequest` rejects every field it
   doesn't model. Agent frameworks send several provider-valid fields
   (`response_format`, `max_completion_tokens`, `store`, `metadata`,
   `reasoning_effort`, the `developer` role, text content-part arrays,
   `refusal` on assistant history messages), so requests fail before a
   budget is ever involved.

## Decision

### Reservations (admission is atomic)

For every budget that matches a request (global, project, `work_id`):

    available = limit − committed spend − outstanding reservations

- **Committed spend** is unchanged: priced receipts in the budget's
  window (ADR-0015).
- **Outstanding reservations** live in a new `budget_reservations` table
  in the budgets SQLite file. Each row holds the request id, the
  request's `project`/`work_id` attribution, an amount, a state
  (`active` or `held`) and a creation time. A reservation counts against
  every budget whose scope matches that attribution and whose window
  contains its creation time.
- **Admission** runs in a single `BEGIN IMMEDIATE` transaction on the
  budgets database. It reads the matching budgets, committed spend and
  outstanding reservations, refuses (`INFERRAIL_E010`, HTTP 402) if any
  block-mode budget would go over, and otherwise inserts the reservation
  before the transaction commits. SQLite's write lock serializes
  admissions across tasks, threads and processes, so two requests can't
  both reserve the same remaining dollars.
- The reserved amount is ADR-0015's conservative pre-flight estimate.
  It now also counts tool definitions and `response_format` in the
  prompt estimate, and it uses `max_completion_tokens` when a request
  sends that instead of `max_tokens`.

### Settlement (after the provider call)

A reservation is settled **per provider attempt**. Each retry re-admits
and reserves again, and can itself be refused.

| Outcome of the attempt | Settlement |
|---|---|
| Usage reported and priced (success, or a partial stream whose final usage arrived) | Receipt written first, **then** the reservation is released. There is a brief window in which both are counted, which errs toward refusing and never toward overspending. |
| Provider answered with an HTTP error before any output | Released (the provider rejected the request) |
| Timeout or transport failure (the request may have reached the provider) | **Held** |
| Stream ended, was cancelled, or failed without reported usage | **Held** |
| Usage reported but the cost can't be computed exactly | **Held** |

A **held** reservation keeps counting against the budget at its
reserved amount. It is never shown as the request's cost: the receipt's
`estimated_cost_usd` stays `null`, and the receipt carries a separate
`budget_held_usd` attribute with the amount held. An estimate is never
presented as an exact cost.

### Unknown pricing

With a block-mode budget in scope, a request for a model with no
verified price is **refused before the provider call**
(`INFERRAIL_E012`, HTTP 402). There is no dollar amount to reserve, and
letting the request through would make the ceiling meaningless.
Warn-mode budgets keep logging only. The operator can add a pricing
override for the model.

### Crash and restart

Reservations are persisted, and they are never released automatically
at startup, because another gateway process may still own them. After a
crash, an `active` reservation keeps counting (fail-closed) until its
budget window rolls over. For a `per_work` budget that means the rest of
that work item's life.

A cancelled attempt (for example the request task is cancelled while the
provider call is in flight) is held, not left active. One known gap
remains: ADR-0006 describes a narrow race in which the server discards a
streaming response before it is ever iterated. In that case no settlement
code runs, so the reservation stays `active` and keeps counting
(fail-closed).

### Streaming usage

For a verified OpenAI provider the gateway already requested usage on
streams unless the caller set `stream_options`. It now requests it
whenever the caller didn't set `include_usage` itself, so a caller who
passes other `stream_options` keys still gets a priced receipt. A
caller's explicit `include_usage: false` wins, and that stream is then
**held** as above.

### Request-field policy (`/v1/chat/completions`)

Top-level fields fall into three explicit lists. Anything not listed is
still rejected with `INFERRAIL_E006`: this is not blind pass-through.

- **Interpreted:** `model`, `messages`, `stream`, `stream_options`, `n`
  (1 only), `max_tokens`, `max_completion_tokens` (both also feed the
  reservation estimate), `tools`, `tool_choice`, `parallel_tool_calls`,
  `temperature`, `top_p`, `stop`, `user`.
- **Forwarded unchanged:** `response_format`, `seed`,
  `frequency_penalty`, `presence_penalty`, `logit_bias`, `metadata`,
  `store`, `reasoning_effort`, `verbosity`, `prediction`,
  `prompt_cache_key`, `prompt_cache_retention`, `prompt_cache_options`,
  `safety_identifier`, and `service_tier` when it is `auto` or
  `default`.
- **Rejected with a stated reason:** `service_tier` values other than
  `auto`/`default` (different prices from the catalog's),
  `audio`/`modalities` (audio tokens aren't priced),
  `web_search_options` (per-call search fees aren't priced),
  `logprobs`/`top_logprobs` (the non-streaming response doesn't carry
  them back yet), and the deprecated `functions`/`function_call`.

Messages accept the `developer` role, `name`, `refusal` on assistant
messages, and `content` either as a string or as an array of
`{"type": "text"}` parts. Other part types and unknown message keys are
now rejected; before this change they were silently dropped. A
non-streaming response now also carries the model's `refusal`, so
structured-output clients see it.

## Consequences

- Concurrent requests under one budget can't jointly overshoot it
  through the admission race. Overshoot still has one source: a request
  whose actual cost is higher than its reservation. The estimate is
  conservative, not a proven upper bound (see ADR-0015's
  `DEFAULT_MAX_COMPLETION_TOKENS_ESTIMATE`, and non-English text can
  run above the 3-characters-per-token assumption). Any excess is
  recorded as `budget_overrun_usd`, and later requests are refused. We
  document this as "refuses a request before the provider call when its
  reservation would exceed the remaining budget", not as a guarantee
  that spend can never exceed the limit.
- Unpriced models under a block budget are now refused. Before this
  change they were skipped silently.
- A held reservation can refuse later requests even though the receipts
  show no cost. That is intentional (fail-closed), and the held amount
  is visible on the receipt.
- Clients that sent unknown message keys, or non-text content parts,
  now get a clear error instead of having those keys silently dropped.
