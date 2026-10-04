# Inferrail — Product

> This file is the authoritative source for exact current scope.

**Developer Preview.** The scope below is fully implemented and tested,
but nothing is stable yet — CLI flags, `inferrail.yaml`'s shape, and
receipt/telemetry JSON fields may change without notice before v1.0. The
package version lives in `pyproject.toml` (and on PyPI); it is not
restated here so it cannot drift.

## What it is

Inferrail is an open inference control plane: infrastructure that sits
between an application and the model providers it calls, so operational
decisions (which provider, which model, retry/fallback behavior, what
happened and why) live in configuration and telemetry rather than scattered
across application code.

v0.1 is the first, narrow slice of that: an OpenAI-compatible HTTP gateway
that takes a chat completion request, routes it to one explicitly
configured provider/model via a static policy, executes it, and returns a
compatible response plus a structured local telemetry record of what
happened — plus, as of this slice, a privacy-preserving economic receipt:
what that execution cost, computed from measured usage and verified
pricing, tied to business context the caller attaches. The long-term
thesis this is the first step of: measure → attribute → connect to
outcome → govern → optimize.

**v0.2.0 adds a second, separate product on top of that same substrate:
AP invoice-exception recovery** — see the next section. It is a bounded
decision-and-execution engine for one specific operational question
(retry vs. human review), not a general invoice-processing product, and
it is fully isolated from the gateway and from Work Economics/Economic
Authority below (own module, own storage, no shared code path).

## AP invoice-exception recovery (v0.2.0)

For teams operating an invoice-extraction workflow: decide whether one
eligible extraction exception gets one permitted machine retry or your
established human-review path, execute the retry through a supported
integration, and record the resulting cost and outcome.

- **Package:** `inferrail.ap` (part of the `inferrail` PyPI package, no
  new install for the fixture path; `pip install "inferrail[ap]"` for the
  bundled OpenAI retry adapter).
- **CLI:** `inferrail ap demo|report|outcome|batch`.
- **Hosted API (optional):** `hosted/ap_exceptions/` — decision,
  persistence, and reporting only; retry execution always happens in
  your own process. Self-serve, no-account sandbox credentials
  (isolated, capped, auto-expiring) are available via
  `POST /v1/sandbox` on top of operator-provisioned keys — see
  `hosted/ap_exceptions/README.md` and `docs/adr/0012`.
- **Full contract:** `docs/capabilities/ap-invoice-exception-recovery.md`
  — supported failure types, retry method, validation contract,
  human-review handoff, versioned policy config, identifiers,
  idempotency/ambiguous-execution handling, and the data boundary.
- **Get started:** `docs/PRODUCT.md` doesn't restate this scope a second
  time — see the capability doc and
  `examples/ap_invoice_exception_recovery/`.

Zero customer adoption or savings claims are made about this capability
— see the capability doc's "Pricing and performance assumptions"
section, which labels every dollar figure as an assumption, not a
validated result.

## Who it's for

Developers and small teams running LLM-backed applications who want:

- a single place to point an OpenAI-compatible client, instead of
  provider-specific SDK code sprinkled through the app
- to actually know, per request, what provider/model served it, how long it
  took, and whether it failed — without adding a hosted observability
  vendor
- to know what each customer, workflow, or feature is actually costing
  them in model spend, without storing their users' prompts to figure
  that out
- a foundation they can run entirely on their own machine or
  infrastructure, with no dependency on an Inferrail-operated service

## The problem being solved right now

Applications that call LLM providers directly hard-code operational
decisions (which provider, which model, how to handle a 429) into
business logic. Inferrail v0.1 moves that decision to a deterministic,
inspectable config file and gives you a telemetry record for every request
— the prerequisite for anything smarter later (see "Long-term direction").

## Current scope: what works today

- `POST /v1/chat/completions` — OpenAI-compatible request/response shape
  for single-turn or multi-turn text chat (see limits below). Every
  top-level request field is explicitly categorized, never silently
  dropped (docs/adr/0021-atomic-budget-reservations.md):
  - **Interpreted and forwarded:** `model`, `messages`, `temperature`,
    `max_tokens`, `max_completion_tokens`, `top_p`, `stop`, `stream`,
    `stream_options`, `tools`, `tool_choice`, `parallel_tool_calls`,
    `user` (`user` reaches the upstream provider verbatim, for its own
    abuse monitoring/rate limiting — Inferrail itself never reads it).
  - **Forwarded unchanged, never interpreted or stored:**
    `response_format` (structured outputs), `seed`,
    `frequency_penalty`, `presence_penalty`, `logit_bias`, `metadata`,
    `store`, `reasoning_effort`, `verbosity`, `prediction`,
    `prompt_cache_key`, `prompt_cache_retention`,
    `prompt_cache_options`, `safety_identifier`, `logprobs`,
    `top_logprobs` (the response's `logprobs` object is passed back
    unchanged), and `service_tier` when it is `auto` or `default`.
  - **Rejected with a stated reason (`INFERRAIL_E006`):** `n != 1`;
    other `service_tier` values and `audio`/`modalities`/
    `web_search_options` (billed in ways the pricing catalog doesn't
    cover); the deprecated `functions`/`function_call`.
  - **Anything else** is rejected with `INFERRAIL_E006` naming the
    field, never accepted and quietly ignored.

  Messages accept the `system`, `developer`, `user`, `assistant`, and
  `tool` roles, `name`, `refusal` on assistant messages, and `content`
  as a string or an array of `{"type": "text"}` parts. Other content
  part types and unknown message keys are rejected. A non-streaming
  response carries the model's `refusal` when it returns one.
- Real SSE streaming (`stream: true`): upstream bytes are proxied
  byte-for-byte as they arrive, never buffered and re-chunked. Retries
  only ever happen before the first byte reaches the client — once a
  stream has yielded anything, a later provider failure or client
  disconnect ends that stream and is recorded as `status: "partial"` on
  its `InferenceEvent`/`InferenceReceipt`, never silently retried (which
  would replay already-observed output) and never given a fabricated
  cost: a partial record only carries whatever usage was actually
  measured before the interruption — `null` if none was.
- Tool/function calling: `tools`, `tool_choice`, and `parallel_tool_calls`
  are accepted and passed through to the provider unmodified, including
  parallel tool calls and streamed tool-call deltas. `role: "tool"`
  messages (tool results) are accepted. Inferrail transports tool-call
  semantics — it never executes a tool itself, never parses or
  re-serializes a tool call's `arguments` string (kept byte-exact end to
  end), and never reorders or renames a call.
- Response identity: the non-streaming response's top-level `id`,
  `created`, and `model` are Inferrail's own values — `id` is the same
  `request_id` used to correlate this response with its
  `InferenceEvent`/`InferenceReceipt`/`inferrail report` row, `created` is
  when Inferrail's execution completed, and `model` is the resolved route
  target (or the passed-through model id — docs/adr/0007), not whatever
  the provider itself echoed back. This is deliberate: it's what lets a
  caller correlate a response with its own receipt without a second
  lookup. The provider's own values, when it returned any, are not
  discarded — they're on `inferrail.provider_request_id`/`inferrail.raw_model`
  instead (e.g. OpenAI's `"chatcmpl-..."` id and a dated model snapshot
  like `"gpt-4o-mini-2024-07-18"`), `null` if the provider didn't return
  one or the request failed.
- `GET /health`
- One provider adapter (`OpenAIProvider`) that speaks the OpenAI
  `/chat/completions` wire format — usable against `api.openai.com` or any
  other endpoint that implements the same shape, via `base_url`
- `POST /v1/messages` — a genuinely separate, Anthropic-compatible
  passthrough (real streaming, tool use via passthrough content blocks,
  priced via the catalog) for `type: anthropic`/`anthropic_compatible`
  providers, backed by its own `AnthropicProvider` adapter and execution
  engine — not a translation of `/v1/chat/completions`. See
  docs/adr/0014-anthropic-messages-passthrough.md. This is what lets an
  Anthropic-SDK client point at Inferrail. Claude Code is not supported
  yet: current versions send `thinking`, `context_management`, and
  `output_config`, which this route rejects (see
  docs/integrations.md).
- Static routing: the request's `model` field selects a named route in
  `inferrail.yaml`, which maps to a provider + underlying model
  deterministically. No cost/latency/capability-aware selection.
- Optional model passthrough: if `default_provider` is set in
  `inferrail.yaml`, a `model` that matches no named route is forwarded to
  that provider unchanged instead of being rejected — so an application
  can use any upstream model id (including ones released after this
  version of Inferrail) without a route being pre-registered for it. Named
  routes still take priority. Off by default for an explicit config; on by
  default for the zero-config quickstart path. See
  `docs/adr/0007-model-passthrough-routing.md`.
- Fixed-count retry with linear backoff for transient provider errors
  (timeouts, rate limits, 5xx), configurable per route
- A structured `InferenceEvent` emitted for every request (success or
  failure): request id, route, provider, model, status, latency, token
  counts when available, retry count. No prompt or response content by
  default.
- Two local telemetry sinks: console (structured log line) and a local
  JSONL file. Nothing leaves the machine through them. (The separate
  usage ping sends nothing unless an endpoint is configured; see
  "Usage/presence beacon" below.)
- A payload-free `InferenceReceipt` emitted for every request (success or
  failure): provider, model, token counts, a `Decimal` cost computed from
  measured usage and a verified price, the price's provenance (source +
  verified date), caller-supplied business attribution, latency, retries,
  status. Local JSONL sink by default (`receipts.path`, default
  `./inferrail-receipts.jsonl`); `receipts.sink: sqlite` is a first-class
  alternative (WAL-mode, indexed on `ts`/`work_id`/`project`/`model`) for
  larger installs — `inferrail report`/`transaction`/`work` work
  unchanged against either, and `inferrail receipts import|export` moves
  history between them (see docs/adr/0013-sqlite-receipts-store.md). See
  "Cost and receipts" below.
- Caller-supplied business attribution: `X-Inferrail-Attribute-<Name>` HTTP
  headers (e.g. `X-Inferrail-Attribute-Customer: acme`) are collected into
  a generic `dict[str, str]` and persisted on the receipt. Never forwarded
  to the upstream provider. Generic by design — no fixed vertical-specific
  fields, which already covers correlating receipts across a multi-turn
  agent loop's several inference calls: e.g.
  `X-Inferrail-Attribute-Run: run_123` or `-Agent`/`-Trace`/`-Workflow`
  work today with no new primitive needed.
- `inferrail report --by <provider|model|route|attribute-name>` —
  aggregates local receipts into a simple table: requests, a separate count
  of failed (non-`success`) requests within the group, tokens, total known
  cost, and a separate count of requests with unresolvable pricing (never
  silently folded into the cost total as `$0`). A group is never rendered
  indistinguishably from an all-success group of the same size when one of
  its requests actually failed.
- `inferrail transaction <task-id> [--attribute-name NAME] [--json]` —
  groups every receipt sharing one attribution-attribute value (default
  attribute: `task_id`) into a single `TaskTransaction`: the list of
  contributing receipts, a known-cost total, a separate unknown-cost-event
  count, and an aggregate status (`success` only if every event succeeded,
  `error` only if every event failed, `partial` otherwise). A read-side
  view over existing receipts — attach the same
  `X-Inferrail-Attribute-Task-Id: <id>` header to every request belonging
  to one task, no other setup required. See
  `docs/adr/0008-task-transactions.md`. v0.1 of this primitive: one event
  type (`inference`) — non-LLM resource types are not yet supported (see
  "Explicit non-goals" below).
- `inferrail work outcome <work-id> --status <status>` appends a minimal,
  customer-declared outcome record to local JSONL. `inferrail work <work-id>`
  joins outcome evidence with receipts that share the generic `work_id`
  attribution attribute, and `inferrail work --all` provides a local bulk
  view. Work summaries are derived read-side views: receipt-only and
  outcome-only work remains visible, unknown-priced successful inference is
  counted separately, and no receipt is never displayed as `$0` cost.
- `import inferrail; inferrail.track_task(task_id="...")` — an
  **experimental** Python helper that attaches `X-Inferrail-Attribute-Task-Id`
  to outgoing requests ambiently for the duration of a `with` block or
  decorated function, via a `contextvars.ContextVar` plus an `httpx` event
  hook (`inferrail.attributed_http_client(base_url=...)`/
  `attributed_async_http_client(base_url=...)`, handed to any httpx-based
  SDK client's `http_client=` argument — verified against the real
  `openai` SDK and LangChain's `ChatOpenAI`). `base_url` is required and
  must be the same URL you give your SDK client — the header is attached
  only to requests whose destination matches it, so a client accidentally
  reused against an unrelated, non-Inferrail endpoint never discloses the
  task id to it. Removes the need to thread `task_id` through nested
  function signatures by hand. No gateway/schema change; purely a
  client-side convenience over the header mechanism above. `task_id`
  only, no public API stability commitment yet. See
  `docs/adr/0009-ambient-task-tracking.md`.
- YAML config (`inferrail.yaml`) + environment variables for secrets, with
  loud validation errors
- A CLI: `inferrail serve` (`--quickstart` to skip `inferrail.yaml` and use
  in-memory defaults), `inferrail config check`, `inferrail report`,
  `inferrail transaction`, `inferrail work`, `inferrail demo` (offline, zero-key walkthrough
  of the receipt/report pipeline using a fake provider), `inferrail try`
  (one real request through the same `InferenceEngine` `inferrail serve`
  uses, no config file required — needs `OPENAI_API_KEY`),
  `inferrail verify-payload-free` (introspects the real, running
  `InferenceReceipt` schema at call time and checks that no field is
  named for message content; a schema check, not an audit of stored
  values or logs. See "Verifying privacy claims yourself" below)
- A configless quickstart path: `inferrail try` and `inferrail serve
  --quickstart` both build the same `InferrailConfig` type `inferrail.yaml`
  loads into, just from an in-memory default instead of a file — not a
  second config system, and it never silently writes a config file to disk.
  As of
  `docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md`,
  quickstart registers **both** an OpenAI provider (`OPENAI_API_KEY`,
  passthrough default for `/v1/chat/completions`) and an Anthropic
  provider (`ANTHROPIC_API_KEY`, passthrough default for `/v1/messages`)
  unconditionally — only the one whose key is actually set will succeed a
  real request. Receipts default to `./inferrail-receipts.jsonl`. The
  startup banner prints the exact, copy-pasteable `base_url`/
  `OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL` line for each SDK, and stdout is
  explicitly line-buffered for the whole process so that banner can never
  be silently lost when it isn't a TTY (piped to a file, a container's
  captured logs, ...).
- `inferrail serve --quickstart` also wraps the receipt sink with
  `ConsoleSummaryReceiptSink` (`src/inferrail/receipts/console_summary.py`):
  one compact line per receipt — model, tokens, cost or `unknown`,
  `work_id` if present, and an explicit "no prompt/response ever
  recorded" reminder — printed to stdout the instant each request
  completes. Only installed under `--quickstart`; a self-hosted operator
  running a real `inferrail.yaml` deployment doesn't get an extra,
  unrequested stdout line per production request.
- `inferrail serve --daily-budget-usd AMOUNT` (combinable with
  `--quickstart`, `--app-mode`, both, or neither) creates a global,
  block-mode, daily budget at that limit before serving — the
  quickstart path's one-line way to never overspend on work. Combined
  with `--app-mode`, it reuses that mode's already-sqlite budgets store;
  combined with plain `--quickstart` alone, it switches receipts to a
  dedicated local SQLite file for that run
  (`./inferrail-receipts.db`, distinct from the plain-quickstart JSONL
  default) since budget enforcement structurally requires an indexed
  store — the banner states this plainly.
- Optional shared-secret gateway auth: if `INFERRAIL_GATEWAY_TOKEN` is set,
  `/v1/chat/completions` requires a matching `Authorization: Bearer`
  header. Unset by default (localhost-dev mode) — see README's
  "Configuration" section. Not a user/auth system; a single shared secret.

### Cost and receipts

- Pricing comes from small built-in catalogs — one per verified vendor
  (OpenAI, Anthropic) — of prices independently checked against that
  vendor's own published pricing page, plus an optional `pricing:`
  section in `inferrail.yaml` for operator-declared overrides or
  additions — all forms require an explicit `source` and
  `verified_date`, so a price's provenance is never lost. The exact,
  authoritative lists are `BUILTIN_OPENAI_PRICING` in
  `src/inferrail/pricing/builtin.py` (as of the last verification,
  2026-08-22: `gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4.1-mini`,
  `gpt-4.1-nano`, `gpt-5`, `gpt-5.1`, `gpt-5-mini`, `gpt-5-nano`, `o3`,
  and `o4-mini`) and `BUILTIN_ANTHROPIC_PRICING` in
  `src/inferrail/pricing/builtin_anthropic.py` (as of the last
  verification, 2026-09-14: `claude-fable-5-1`, `claude-opus-5`,
  `claude-sonnet-5`, `claude-haiku-4-5`).
- **Models with context-tiered pricing are deliberately absent**, even
  when they're current flagships (`gpt-5.6-sol`, `gpt-5.6-terra`,
  `gpt-5.6-luna`, and the `-pro` variants). Those bill at a higher rate
  above a context-length threshold, which a single input/output
  `PriceEntry` cannot express — publishing only the short-context rate
  would silently under-report a long-context request. They resolve to
  `null` until either the schema models context tiers or an operator
  declares a `pricing:` override they've chosen themselves. Batch, flex,
  fast, and cached-input tiers are excluded for the same reason. (No
  current Claude model has context-tiered pricing.)
- Each built-in catalog only applies to a provider configured as
  `type: openai`/`type: anthropic` (respectively) with the default
  `base_url` (i.e. verifiably that vendor's own API) — never guessed onto
  an `openai_compatible`/`anthropic_compatible` endpoint that merely
  shares the wire format, since that could be serving a completely
  different, differently-priced model under a colliding name. See
  `docs/adr/0005-privacy-preserving-economic-receipts.md` and
  `docs/adr/0014-anthropic-messages-passthrough.md`.
- Anthropic prompt caching: cache writes and reads are part of
  `prompt_tokens` (total input) and are also recorded separately on the
  receipt (`cache_creation_input_tokens`, `cache_creation_5m_input_tokens`,
  `cache_creation_1h_input_tokens`, `cache_read_input_tokens`). They are
  priced at the cache rates in the price entry (the built-in Anthropic
  catalog includes them; an operator `pricing:` entry can declare
  `cache_write_5m_usd_per_million`, `cache_write_1h_usd_per_million`,
  `cache_read_usd_per_million`). If a cache rate is missing, or cache
  writes arrive without the 5-minute/1-hour split, the cost is `null`,
  never a cost that leaves cache tokens out.
- OpenAI cached input (`prompt_tokens_details.cached_tokens`) is not
  modeled yet: it is priced at the full input rate, so a cached OpenAI
  request's cost is overstated, not understated.
- Unresolvable pricing (unknown model, or an
  `openai_compatible`/`anthropic_compatible` provider with no override
  configured) leaves `pricing`/`estimated_cost_usd` explicitly `null` on
  the receipt — never a fabricated `$0`.
- All money arithmetic uses `Decimal`, never `float`.

### Budgets and enforcement

See `docs/adr/0015-budget-enforcement.md` for the full design; summary
here:

- Off by default (`budgets.enabled: false` in `inferrail.yaml`).
  Turning it on requires `receipts.sink: sqlite` — enforcement computes
  spend-so-far from the indexed receipts store (docs/adr/0013), and
  Inferrail refuses to load a config that claims enforcement without
  that store to check against.
- A budget is scoped `global`, `project`, or `work_id` (matched against
  the request's `X-Inferrail-Attribute-*` headers — see "Attribution"
  below), over a window (`per_work`, `daily`, `monthly`), in `warn` or
  `block` mode, with a `limit_usd`. Manage them with `inferrail budget
  set|list|rm`.
- `block` mode rejects a request with HTTP 402 *before any provider is
  contacted* when its reservation would exceed the remaining budget.
  Remaining = `limit_usd` − committed (priced) spend − outstanding
  reservations of other in-flight or held requests; admission reserves
  atomically (one SQLite write transaction on the budgets file), so
  concurrent requests — parallel tool calls, sub-agents, several gateway
  processes on one budgets file — can't each spend the same remaining
  dollars (docs/adr/0021-atomic-budget-reservations.md). The reserved
  amount is a conservative catalog-based estimate (never a real token
  count — Inferrail has no tokenizer dependency) that counts messages,
  tool definitions, and `response_format`, plus `max_completion_tokens`
  / `max_tokens` (or a fixed 4096 when neither is sent). The response
  body's `error.details` carries `budget_id`/`limit_usd`/
  `spent_so_far_usd`/`reserved_usd`/`estimated_request_usd` so a
  caller can react programmatically, not just read a message.
- Each provider attempt (including each retry) is admitted on its own.
  When it ends, its reservation is released if its cost is known (after
  the receipt is written) or the provider answered with an HTTP error;
  it is **held** — still counted against the budget — if the provider
  may have billed but no priced usage came back (a timeout, a dropped
  connection, a stream without usage, a cancelled stream). A held
  amount is never shown as cost: the receipt's cost stays `null` and it
  carries a separate `budget_held_usd` attribute. Reservations persist
  across restarts and are never released automatically (fail-closed);
  they stop counting once the budget's window rolls over.
- **Per-run budgets without pre-registration**
  (docs/adr/0022-per-run-budget-declaration.md): a request carrying
  `X-Inferrail-Attribute-Work-Id` can declare its run's ceiling with
  `X-Inferrail-Budget-Usd: 0.50`, or inherit `budgets.per_work_default_usd`.
  The run's `per_work` block budget is created on first use, inside the
  same atomic admission as the reservation, so concurrent first requests
  for a new run can't slip past it.
  - The first budget for a work_id wins. A later request declaring a
    different amount is refused (HTTP 400, `INFERRAIL_E013`), and a
    header never loosens any other budget.
  - `budgets.per_work_max_usd` caps declarations (refused, not clamped),
    and `budgets.allow_declared_budgets: false` turns them off.
  - Invalid values, a declaration without a work_id, and a declaration
    sent while budgets are disabled are refused, never ignored.
  - The header is never forwarded upstream.
  - Sub-agents share a run's budget by reusing its work_id; there's no
    budget hierarchy.
  - Each run leaves one budget row. Admission stays fast as rows
    accumulate (indexed lookup), and there's no automatic cleanup yet
    (`inferrail budget rm` removes one).
- **In front of an existing OpenAI-compatible gateway** (docs/adr/0022;
  tested locally in front of LiteLLM and otari, other gateways untested): on an `openai_compatible` provider,
  `price_as: openai|anthropic` applies that vendor's list-price catalog
  (operator-asserted, and recorded as such on the price source), and
  `request_stream_usage: true` asks the upstream for stream usage. Both
  are opt-in. Each gateway enforces only its own budgets. A downstream
  budget/quota refusal is returned as `INFERRAIL_E014` (HTTP 402, not
  retried), not as a rate limit.
- With a `block` budget in scope, a model with no verified price is
  refused (HTTP 402, `INFERRAIL_E012`) — there's no amount to reserve.
  Add a `pricing:` override to use it.
- Overshoot semantics: the admission race is closed, but a request
  whose *actual* cost exceeds its reservation (e.g. no `max_tokens`
  and a longer answer than the 4096-token assumption, or text that
  tokenizes denser than the estimate assumes) still completes; the
  excess is recorded as `budget_overrun_usd` and later requests are
  refused. This is a pre-call ceiling on reservations, not a guarantee
  that spend can never exceed the limit. The block itself still
  produces a normal (costless) receipt carrying a `budget_id` attribute
  (`budgets.enforcement.augment_attributes_with_block`), so it's visible
  in `inferrail report` like any other rejected request, *and*
  distinguishable from an unrelated failure — this is what the dashboard's
  Budgets screen (below) uses to build an honest blocked-request log.
- `warn` mode never blocks. If a request's *actual* cost (known only
  after completion) pushes a matching budget over its limit, the
  receipt gains a `budget_overrun_usd` attribute — the same generic
  attribution mechanism customer/project/work_id already use, not a new
  field — so the overrun is honestly recorded, never silently absorbed.
- Shared as-is between `/v1/chat/completions` and `/v1/messages` — a
  budget applies regardless of which wire format a request arrives on.

### Local control API and `--app-mode`

See `docs/adr/0016-local-control-api.md` for the full design; summary
here — this is a *local*, single-process API (not the hosted, cross-
fleet "control plane" `docs/adr/0004` anticipates):

- `inferrail serve --app-mode` loads `inferrail.yaml` as normal
  (providers/routes/telemetry untouched), then relocates receipts and
  budgets under the OS-conventional per-user app-data directory,
  forcing `receipts.sink: sqlite` and `budgets.enabled: true`
  regardless of what the file says, and mounts a second HTTP surface —
  `/v1/local/*` — guarded by a mandatory per-install token (printed on
  startup, also saved under the app-data directory). **Combinable with
  `--quickstart`** as of ADR-0020 — quickstart still supplies
  providers/routes; `--app-mode` still relocates receipts/budgets/
  dashboard the same way it does with a real `inferrail.yaml`. This is
  the fastest path to seeing a receipt land in the dashboard's Live Feed
  with zero config file.
- `GET /v1/local/receipts` (paginated, filterable by
  work_id/project/model), `GET /v1/local/work` / `GET
  /v1/local/work/{work_id}` (rollups), `GET /v1/local/budgets` / `POST
  /v1/local/budgets` / `DELETE /v1/local/budgets/{id}` (CRUD, shared
  live with `BudgetEnforcer` — no second view of the same data), and
  `GET /v1/local/stream` (SSE tail of newly-emitted receipts).
- Meant to be consumed by the dashboard (v0.4.0, below), not by a human
  directly — `inferrail report`/`work`/`budget` remain the CLI's own
  read/write surface either way.

### Dashboard (v0.4.0) — all six screens built

A static, local web SPA in `app/`, served by the same process as the
local control API when `--app-mode` is on — see
`docs/adr/0017-dashboard-in-app-directory.md`.

- `inferrail serve --app-mode` prints a ready-to-open URL —
  `http://<host>:<port>/dashboard/?token=<token>` — that already carries
  the per-install local-API token, so opening it is the only step: no
  copy-pasting a token into a settings field, no second terminal command.
- **Live Feed** (built): every receipt this install produces, streamed
  in as it happens over `GET /v1/local/stream`. An unknown cost renders
  as the word "unknown", styled distinctly from a real, known `$0.0000`
  — never collapsed into the same thing. A request a budget refused
  reads "blocked by budget" and "not sent" instead.
- **Work** (built): cost per `work_id`, over `GET /v1/local/work`;
  clicking a row drills into `GET /v1/local/work/{work_id}` for its full
  rollup (receipt count, status, outcome, started/ended timestamps). The
  drill-down leads with what the work item spent, its own per-work
  budget if it has one, and how many of its requests a budget blocked
  (counted with `GET /v1/local/receipts?work_id=…&status=error`). A
  `work_id` with some priced receipts and some unpriceable ones shows
  both — e.g. `$0.0007 (+2 unknown)` — never a single misleading total.
  Client-side routing is hash-based (`#/work`, `#/work/<id>`), so the
  drill-down is a real, bookmarkable/back-button-able URL without the
  server needing a SPA catch-all route.
- **Budgets** (built): one card per budget leading with Spent, Budget,
  and how many requests it blocked, in plain wording ("Work item budget
  · blocks requests over the limit") with the budget id in small type;
  create/remove budgets (`POST`/`DELETE
  /v1/local/budgets`), a burn bar per budget over `GET
  /v1/local/budgets/spend` (reuses `budgets.enforcement.spent_so_far_usd`
  directly — the exact function `BudgetEnforcer.reserve` itself uses, so
  the bar can never drift from what enforcement actually computed), and
  a blocked-request log. The log is real, not inferred from error text:
  a pre-flight block now stamps the receipt's `attributes.budget_id`
  (`budgets.enforcement.augment_attributes_with_block`, the same pattern
  `augment_attributes_with_overrun` already used) so the dashboard can
  tell a genuine budget block apart from any other `status: "error"`
  receipt. `GET /v1/local/receipts` gained an optional `status` filter
  to support this. The log and the per-budget counts read the newest
  1,000 error receipts; a count is shown as "N+" when older ones exist
  beyond that.
- **Recover** (built): the pending human-review queue —
  `GET /v1/local/ap/pending` (every work_id whose AP invoice-exception
  decision is `awaiting_human_review`, built from
  `ap.report.build_live_report`, the same auditable report `inferrail ap
  report` and the hosted API's `GET /v1/report` already produce) — with
  a one-click "record outcome" (`POST /v1/local/ap/{work_id}/outcome`,
  the same store call `inferrail ap outcome` makes). **Requires an AP
  recovery store** — `inferrail serve --app-mode` always provisions one
  at a fixed app-data path (printed on startup, alongside the receipts/
  budgets paths) and prints the exact `--db` value to point `inferrail
  ap demo|report|outcome` at to populate it; override the path with
  `INFERRAIL_AP_DB` if you already have one elsewhere. An empty store
  (the common case for anyone not using the AP module) shows "nothing
  pending review," never an error.
- **Connect** (built): copy-paste snippets (curl, Anthropic SDK env
  var, the Anthropic Messages API, the OpenAI Python SDK,
  LangChain) with copy buttons, each generated against
  `window.location.origin` — since the dashboard is served by the exact
  same process as the gateway, these are copy-paste-correct for *this*
  running install, not a generic `127.0.0.1:8000` placeholder. Snippets
  that need an Anthropic route configured say so plainly rather than
  implying they always work.
- **Settings** (built): a real "Export" (`GET /v1/local/receipts/export`,
  streams the exact JSONL shape `inferrail receipts export` produces,
  generated from `ReceiptsStore.read_all()` directly rather than a
  server-side temp file); a real "Pricing catalog" freshness view
  (`GET /v1/local/pricing/freshness`, the same
  `cli.pricing.catalog_freshness` computation `inferrail pricing
  update`/`doctor` already share — never a network fetch); and a real
  "Usage ping" toggle (`GET`/`POST /v1/local/usage-ping`) — see below.

### Usage/presence beacon (opt-out)

A real feature, not a placeholder (`src/inferrail/usage_ping/`,
`docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md`,
`docs/privacy/usage-ping.md`). **On by default** (a deliberate reversal
of the original opt-in design — see ADR-0020, which supersedes
`docs/adr/0019-opt-in-usage-ping.md`'s "default off" only), but still
inert with no `usage_ping.endpoint` configured in `inferrail.yaml`
regardless of the toggle — Inferrail ships with no built-in default
endpoint, so an install is never able to send anything until an operator
explicitly configures one. Fires for every `inferrail serve` invocation
now, not just `--app-mode` (quickstart and plain config-based deployments
included). Four lifecycle events only: `install` (once ever),
`serve_start` (every process start), `first_receipt` (once ever),
`heartbeat` (at most once per 24 hours while serving). Never a prompt,
response, model name, cost, work_id, project name, or anything about
actual traffic. Sending never blocks, slows, or can fail the gateway —
it happens on a best-effort background thread with a short timeout, and
any failure (offline, unreachable, timeout) is swallowed.

Turned off with `inferrail telemetry disable`, the Settings screen's
toggle, `INFERRAIL_TELEMETRY=0`, `serve --no-telemetry`, or
`DO_NOT_TRACK=1` — and automatically under common CI environment
variables and this project's own test suite, no configuration needed.

Two ways to verify what would be sent without trusting this
documentation: `inferrail telemetry preview` (prints the exact payload
for every event, from this install's real id/OS/version, without
sending anything) and the Settings screen's own link to
`docs/privacy/usage-ping.md`. `inferrail telemetry status|enable|disable`
work standalone, without `--app-mode` or even an `inferrail.yaml`.

A reference collector (`hosted/usage_ping/`) is built — own process, own
SQLite storage (an `installs` table tracking activation via
`reached_first_receipt_at`, plus an append-only `events` log), zero
dependency on the `inferrail` package, never logs or persists the
connecting IP address, admin-gated aggregate `/stats` (disabled entirely
unless an admin token is set). `scripts/owner_stats.py` reads PyPI
download counts (always, no telemetry needed) plus, if pointed at a
deployed collector's database with `--db`, real install/activation/
active-user numbers. Deploying a collector instance and configuring
`usage_ping.endpoint` to point at it is a human action — see
`PROGRESS.md`'s "HUMAN ACTION NEEDED".
- **The dashboard is bundled into every wheel this project's CI
  builds — including the actual PyPI-published artifact and the
  three-OS `platform-verify.yml` wheels.** A hatchling build hook
  (`hatch_build.py`) runs the dashboard build and packages the result
  as `inferrail/dashboard_static/` inside the wheel; see
  `docs/adr/0018-dashboard-wheel-packaging.md`. Building from a
  checkout with no Node installed, or from the sdist, still works and
  just ships without a dashboard, exactly as before — never a build
  failure. `publish.yml` and `platform-verify.yml` both set up Node
  (`actions/setup-node@v4`) and assert the built/installed wheel
  actually contains the dashboard, so a real `pip install inferrail`
  release is verified to ship a working dashboard before it's ever
  published, not just this project's own separate `dashboard` CI job.

### Diagnostics: `inferrail pricing update` and `inferrail doctor`

- `inferrail pricing update` never fetches anything over the network —
  there's no way to do that and still meet this project's own bar for a
  verified price (checked by hand against the vendor's own pricing
  page, `docs/adr/0005`). It reports each built-in catalog's age and
  states the real fix: `pip install --upgrade inferrail`, or an
  explicit `pricing:` override.
- `inferrail doctor` checks port availability, pricing-catalog
  freshness, and provider reachability (a bare TCP connect — never an
  HTTP request, never using a real API key), each with a one-line fix.

### Explicit non-goals / not yet supported

Not a hidden limitation — these are the honest edges of v0.1:

- Multiple choices (`n != 1`) is rejected
- Audio / file message content — only string content, `{"type": "text"}`, `{"type": "refusal"}`
  parts and `{"type": "image_url"}` parts (images reserve a fixed
  3,000-token estimate each under a budget; gpt-4o-mini can bill a
  high-detail image above that, in which case that one call's overrun is
  recorded and the run's later calls are refused)
- Cost estimates for anything outside the built-in catalog or an explicit
  operator `pricing:` override — an unrecognized (provider, model) always
  produces `null`, never a guessed cost (see "Cost and receipts" above)
- Time-to-first-token — always `null`. Streaming now exists, so this is
  measurable in principle (the first upstream chunk's arrival is already
  observed internally), but Inferrail doesn't populate it yet — a
  natural, low-risk follow-up once there's a reason to, not silently
  guessed in the meantime
- Any provider wire format other than OpenAI-compatible
  (`/v1/chat/completions`) or Anthropic-compatible (`/v1/messages`, see
  docs/adr/0014-anthropic-messages-passthrough.md) — Gemini, Bedrock's
  native API, etc. are still not supported
- Intelligent/adaptive routing of any kind
- Historical price versioning (a receipt embeds the price snapshot used at
  the time, but there is no queryable price-history store)
- Any hosted/cloud component — see "OSS vs. hosted" below
- Non-LLM economic events (browser, search, compute/sandbox, MCP tool
  cost) in `TaskTransaction` — its only event type today is `inference`;
  see `docs/adr/0008-task-transactions.md`
- Outcome or business-value linkage (success/failure signal, revenue,
  margin) on a `TaskTransaction` — it aggregates cost only. The separate
  `work` command accepts only a minimal caller-declared status; it does not
  model revenue, margin, business payloads, or a work lifecycle.
- Any policy decision at the *transaction* level — `inferrail
  transaction` only reports, it never blocks a request. (Budget
  enforcement itself now exists, at the gateway — see "Budgets and
  enforcement" above — this bullet is about the separate, reporting-only
  `transaction` command specifically.)

## Verifying privacy claims yourself

The claim being checked: Inferrail's receipt and telemetry paths do not
copy prompt or response bodies into the records they write. Three ways
to check it, each with a different scope:

1. `inferrail verify-payload-free` lists every `InferenceReceipt` field
   and checks that none is named for message content. It inspects the
   schema only. `attributes` is a free-form `dict[str, str]` stored as
   the caller sends it, so a name check cannot prove that stored values
   are free of sensitive text.
2. The canary tests send marker strings through the real gateway paths
   (success, provider error, streaming, tool calls, both
   `/v1/chat/completions` and `/v1/messages`) and assert the markers
   never reach a receipt or telemetry event:
   `tests/unit/test_gateway_receipts.py`, `tests/unit/test_gateway.py`,
   `tests/unit/test_gateway_anthropic.py`.
   The code those tests exercise: request handlers
   (`src/inferrail/gateway/routes.py`), execution engines
   (`gateway/execution.py`, `gateway/anthropic_execution.py`), provider
   adapters (`providers/openai.py`, `providers/anthropic.py`), the
   receipt builder (`receipts/builder.py`), and the sinks
   (`receipts/sinks.py`, `receipts/sqlite_store.py`), all under
   `src/inferrail/`.
3. Against your own running gateway:

In `inferrail.yaml`, set:

```yaml
telemetry:
  sink: jsonl
  path: inferrail-telemetry.jsonl
```

Restart `inferrail serve`, then:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "default", "messages": [{"role": "user", "content": "MARKER-1234-do-not-persist-me"}]}'

grep -c "MARKER-1234" inferrail-telemetry.jsonl   # 0, every time
cat inferrail-telemetry.jsonl                     # latency, tokens, status — no message content

grep -c "MARKER-1234" inferrail-receipts.jsonl    # 0, every time (receipts are on by default)
cat inferrail-receipts.jsonl                      # tokens, cost, pricing — no message content
```

This checks only what Inferrail itself writes to disk for that request.
It does not cover values you put in attribution headers, your own
application or proxy logs, or the provider: your provider still receives
the real prompt. Inferrail is a pass-through gateway to it, not a privacy
boundary against it. None of these checks is a security audit.

## Hosted capabilities (beyond the self-hosted data plane)

Everything above is the self-hosted gateway: zero dependency on any
Inferrail-operated service (see "Who it's for" and `docs/adr/0004`).

**Hosted cost gateway trial** (`hosted/cost_gateway/`): a no-account
trial at [tryinferrail.com/try/](https://tryinferrail.com/try/) that
issues a short-lived, isolated tenant with zero-key demo traffic, and
optionally proxies real traffic through a visitor's own OpenAI or
Anthropic key. With a real key, the hosted process holds that key in
memory and handles the traffic; the trial expires within 4 hours of the
key being added (24 hours at most in demo mode). Preview status. Full
contract and key-handling threat model:
`hosted/cost_gateway/README.md`.
Inferrail helps companies measure, attribute, and eventually govern the
economics of work performed by AI agents. The gateway and receipts above
are today's working measurement layer. Separately, Inferrail also
operates two hosted, paid capabilities that extend that foundation
toward machine buyers. Both are experimental and Base Sepolia testnet
only; neither controls external wallets, providers, or network
spending:

- **Inferrail Work Economics** (`hosted/work_economics/`) — given
  caller-declared economic events for a unit of AI work, returns a
  normalized cost summary and a commercial receipt, paid for over x402.
  Base Sepolia testnet only right now. Full contract:
  `docs/capabilities/work-economics.md`. Why this lives outside the
  package installed by `pip install inferrail`: `docs/adr/0010`.
- **Inferrail Economic Authority** (`hosted/a2a_economic_authority/`):
  explores voluntary coordination of a caller-declared spending
  boundary between agents. The boundary is caller-declared and the
  ledger is cooperative; Inferrail records and coordinates it within
  its own service and does not control external wallets, providers, or
  network spending. This is not real-world spend enforcement. Base
  Sepolia testnet only. Full contract:
  `docs/capabilities/economic-authority.md`.

## Long-term direction

The progression Inferrail is built to support, in order, is:

observable → controllable → measurable → comparable → optimizable →
increasingly intelligent

Each step needs the one before it to be trustworthy first. v0.1 delivered
the first step (observable: a real telemetry record for every request)
and the scaffolding for the second (controllable: explicit routing
config, provider abstraction). This slice takes the first real step into
measurable: a per-request economic receipt (verified pricing, `Decimal`
cost, business attribution) and a local report to read it back — still a
single-process, single-machine capability, not fleet-wide history. Later
phases — comparing providers/models on cost and quality across many
requests, recommending routing policies, fleet-wide analytics — depend on
operational history accumulating across many requests/deployments, which
is naturally a *hosted* capability once a user wants it to span more than
one machine or process. See `ARCHITECTURE.md` for how the OSS data plane
and a future hosted control plane are meant to stay decoupled.

## Non-goals (for the project generally, not just this session)

Inferrail is not attempting to become:

- A LangChain/LiteLLM-style all-in-one framework
- A prompt-management, RAG, or agent framework
- A vector database
- An evaluation platform (though evaluation could plausibly build on top of
  its telemetry later)
- A hosted-only product — the data plane must remain genuinely useful with
  no Inferrail-hosted service required
