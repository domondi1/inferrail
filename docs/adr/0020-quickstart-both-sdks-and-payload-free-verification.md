# 0020. Lead with the payload-free cost-receipt promise: dual-SDK quickstart, `verify-payload-free`, quickstart budgets, and an opt-out usage beacon

## Status

Accepted

## Context

An audit of the live site and the first-run experience (this session,
2026-09-18) found that the product and the website had drifted away from
Inferrail's own founding claim -- "know what your AI work costs, without
keeping what it said" -- toward leading with the newer AP
invoice-exception-recovery capability instead. The founder asked for the
payload-free cost-receipt story to become the single most prominent thing
on the site and in the product's first-run experience, with every
existing capability (AP, Work Economics, budgets, the dashboard, MCP)
reorganized to sit beneath and support it, not removed.

Concretely, the audit found:

- `inferrail serve --quickstart` never prints a copy-pasteable
  `base_url` for either SDK, and only ever configures an OpenAI
  provider -- an Anthropic SDK client has no route to pass through to.
- Its own startup banner can be silently lost entirely whenever stdout
  isn't a TTY (`print()` with no `flush=True`, block-buffered once
  redirected to a file/log/container capture).
- Budgets are structurally impossible to combine with `--quickstart`
  (budget enforcement requires `receipts.sink: sqlite`; quickstart
  hardcodes `jsonl`; the one mode that forces `sqlite`, `--app-mode`,
  was flagged as explicitly incompatible with `--quickstart`).
- There is no command that proves the payload-free guarantee from the
  running schema, suitable for pasting into a security review --
  despite the guarantee itself being real and already regression-tested.
- `inferrail demo` wrote (and `.unlink()`'d!) the *real* default work-
  outcomes file a genuine user's own `inferrail work outcome` records
  live in -- a real data-loss bug, not just noise.
- Most of the "owner-side install/usage counting" the founder separately
  asked for already exists (`src/inferrail/usage_ping/`,
  `hosted/usage_ping/`), but built to ADR-0019's **opt-in, default-off**
  spec -- directly citing `MISSION.md`'s own "no telemetry without
  explicit opt-in (default off)" non-negotiable.

The founder was asked explicitly, given that conflict, whether to keep
the opt-in default or switch to opt-out. **Explicit decision: switch to
opt-out (on by default).** This is a deliberate reversal of ADR-0019's
"default off," recorded here plainly rather than silently changed —
ADR-0019 itself remains in place as the historical record of the
original decision and its reasoning; this ADR supersedes only its
default-posture conclusion, not the rest of its design (the payload
shape discipline, the no-baked-in-endpoint safeguard, the fail-silent
networking, the independently-verifiable `telemetry preview` command all
carry forward unchanged).

## Decision

### 1. Quickstart speaks both SDKs

`config/quickstart.py`'s `build_quickstart_config()` now registers two
providers -- `openai` (`OPENAI_API_KEY`) and `anthropic`
(`ANTHROPIC_API_KEY`) -- each the passthrough default for its own wire
format. Doing this correctly required a real (small, backward-compatible)
config change: `InferrailConfig` gains `default_anthropic_provider`,
separate from the existing `default_provider`, because the two pipelines
(`/v1/chat/completions` vs `/v1/messages`) each build their own `Router`
against disjoint provider sets (`providers.registry.build_providers` vs
`build_anthropic_providers`) -- one shared default could never correctly
passthrough for both at once. `gateway/app.py` now constructs two
`Router` instances instead of one, sharing `config.routes` but using each
pipeline's own default-provider field. Existing configs that only set
`default_provider` are unaffected; `default_anthropic_provider` defaults
to `None`, matching prior behavior (an explicit named route is required
for Anthropic passthrough, exactly as before) unless a config opts in.

### 2. The quickstart banner is fixed and complete

`_cmd_serve` now line-buffers stdout for the whole process
(`sys.stdout.reconfigure(line_buffering=True)`, guarded by an
`isinstance` check rather than a `# type: ignore`) before printing
anything, so the banner can no longer be silently lost when stdout isn't
a TTY. The banner itself now prints the exact, copy-pasteable
`base_url`/`OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL` lines for both SDKs,
the receipts path and how to read it (`inferrail report`/`inferrail
work`), and a pointer to `inferrail verify-payload-free`.

### 3. `inferrail verify-payload-free`

A new command (`cli/verify.py`) that introspects the real, running
`InferenceReceipt.model_fields` at call time -- never a hardcoded string
-- lists every field, checks it against the same payload-capable-name
set (`prompt`/`messages`/`content`/`response`) the existing
`test_inference_receipt_has_no_payload_fields` regression test already
enforces, and prints a plain PASS/FAIL statement plus the honest scope
caveat (the provider still receives the real prompt; this is a
receipt-storage guarantee, not a network privacy boundary). Suitable for
pasting into a security review as-is.

### 4. Quickstart budgets

`serve --daily-budget-usd AMOUNT` (combinable with `--quickstart`,
`--app-mode`, both, or neither) creates a global, block-mode, daily
budget before the server starts. Combined with `--app-mode`, it reuses
that mode's already-sqlite budgets store; combined with plain
`--quickstart` alone, it switches quickstart's receipts to a dedicated
local SQLite file for that run (`./inferrail-receipts.db`, distinct from
the plain-quickstart JSONL default so neither format silently shadows
the other in `inferrail report`'s own default lookup) -- budget
enforcement structurally requires an indexed store, the same requirement
`--app-mode` already has. The banner states this plainly rather than
leaving a silent format switch for the user to discover later.

### 5. `--quickstart` and `--app-mode` are no longer mutually exclusive

The prior CLI rejected `--quickstart --app-mode` together. There was no
real reason for this: quickstart supplies providers/routes; app-mode
relocates receipts/budgets under the OS app-data directory and mounts
the dashboard + local control API. These are independent axes. Combining
them gives a user the dashboard's Live Feed (receipts arriving live) on
top of the zero-config quickstart path with one extra, well-documented
flag, without requiring a real `inferrail.yaml`.

### 6. `ConsoleSummaryReceiptSink`

A new `ReceiptSink` wrapper (`receipts/console_summary.py`, mirroring the
existing `UsagePingReceiptSink` wrapping pattern) prints one compact line
per receipt -- model, tokens, cost or `unknown`, `work_id` if present,
and an explicit "no prompt/response ever recorded" reminder -- to stdout.
Installed only for `--quickstart`, so a self-hosted operator running a
real `inferrail.yaml` deployment doesn't get an extra, unrequested stdout
line per production request.

### 7. `inferrail demo` no longer touches real outcome data

`cli/demo.py` now writes its synthetic work-outcome rows to a dedicated
`./inferrail-demo-work-outcomes.jsonl`, never
`cli.work.DEFAULT_OUTCOMES_PATH` (`./inferrail-work-outcomes.jsonl`) --
the file a real user's genuine `inferrail work outcome` records live in.
The prior code both wrote *and unconditionally `.unlink()`'d* that shared
default at the start of every demo run, so running the demo after doing
real work could silently delete real outcome history. Regression test:
`test_demo_never_touches_the_real_default_outcomes_path`.

### 8. The usage-ping beacon: opt-out by default, four events, `python_version` added, always fires

Per the founder's explicit decision above:

- `UsagePingConfig.enabled` now defaults to `True` (was `False`).
  `usage_ping.state.load_state`'s own `default_enabled` parameter default
  changed to match. **Unchanged:** still fully inert with no
  `usage_ping.endpoint` configured, regardless of `enabled` -- there is
  still no built-in default endpoint baked into this package.
- Event names and payload fields now match the founder's exact spec:
  `install` (once-ever, replaces `first_run`), `serve_start` (fires every
  process start, never deduped -- new), `first_receipt` (once-ever,
  unchanged in spirit but no longer paired with a separate
  `tool_connected`), `heartbeat` (at most once per 24 hours while
  serving -- new). `budget_created` is retired -- it doesn't appear in
  the founder's four-event spec, and creating a budget is ordinary local
  API traffic now, not a usage-ping milestone.
- Payload fields: `install_id`, `event`, `version` (renamed from
  `inferrail_version`), `os`, `python_version` (new, major.minor only,
  e.g. `"3.12"` -- never a full patch/build string). The `ts` field is
  dropped: the collector stamps `seen_at`/`last_seen_at` itself and never
  trusts a client clock.
- **Fires for every `inferrail serve` invocation now**, not just
  `--app-mode` -- `UsagePingReceiptSink` and the `install`/`serve_start`
  calls in `gateway/app.py` moved out of the `if app_mode:` block.
  Storage location (install id, on/off state, sent-event markers) follows
  `budgets.path`'s parent directory, same as before -- the real OS
  app-data directory under `--app-mode`, or `budgets.path`'s own parent
  (cwd by default) otherwise, matching every other quickstart/plain-serve
  file convention.
- **Heartbeat mechanism:** `usage_ping.state.mark_heartbeat_sent_if_due`
  gates on wall-clock time (≥24h since the last one actually sent, not
  once-ever), and `usage_ping.client.start_heartbeat_thread` starts a
  best-effort daemon thread that wakes roughly hourly to check it --
  started only when `usage_ping.endpoint` is actually configured and the
  environment doesn't disable telemetry (see below), so the overwhelming
  majority of installs and every test/CI run never spawn this thread at
  all, not just never send from it.
- **New environment-based opt-out gate**
  (`usage_ping.client._disabled_by_environment`), checked before every
  send and before the heartbeat thread is even started:
  `INFERRAIL_TELEMETRY=0`, `DO_NOT_TRACK=1` (the
  https://consoledonottrack.com/ convention), `CI` set to a truthy value,
  or running under this project's own test suite
  (`PYTEST_CURRENT_TEST` -- set automatically by pytest, no special
  fixture needed). `serve --no-telemetry` sets `INFERRAIL_TELEMETRY=0`
  in-process before the app is built, reusing the same gate rather than
  adding a second code path.
- **Collector schema** (`hosted/usage_ping/service.py`) rewritten to the
  founder's exact two-table shape: `installs` (one row per install,
  upserted on every beacon, `reached_first_receipt_at` set once on the
  first `first_receipt` event and never overwritten after) and `events`
  (append-only, one row per beacon, `event` constrained to the four known
  values). Still: no IP address column anywhere, no auth required to
  submit a ping, `extra="forbid"` server-side schema enforcement, a
  per-IP rate limit, a request-size cap, and a kill switch. SQLite here;
  the same shape is a straight swap to Postgres (see the module
  docstring) if a future deployment needs it.
- **New `scripts/owner_stats.py`** -- zero-telemetry PyPI download counts
  (pypistats.org, public, no auth, always run) plus, if `--db` is given,
  the exact queries the founder specified against the collector's own
  database (total installs, activated, active 7d/30d, new installs per
  ISO week, activation rate).

## Consequences

- `ConsoleSummaryReceiptSink` and the dual-provider quickstart config are
  new, small, additive surfaces -- no existing `inferrail.yaml`-driven
  deployment's behavior changes unless it explicitly opts into the new
  `--daily-budget-usd`/`--no-telemetry` flags or sets
  `default_anthropic_provider`.
- The usage-ping default-posture reversal is the one genuinely
  user-visible behavior change for **existing** installs that already
  set `usage_ping.endpoint` in their own `inferrail.yaml`: those installs
  now beacon by default instead of requiring a separate opt-in, unless
  they also set `usage_ping.enabled: false` or use one of the new
  opt-out mechanisms. No install without an explicitly configured
  endpoint is affected at all -- the mechanism stays fully inert for the
  overwhelming majority of the userbase, exactly as ADR-0019 designed.
- Whether to ship a real, Inferrail-operated collector endpoint as this
  package's baked-in default remains a separate, later decision --
  unchanged from ADR-0019's own "Consequences" section. This ADR does
  not make that call.
- `docs/privacy/usage-ping.md` and `docs/PRODUCT.md`'s usage-ping section
  are updated in the same change to describe the new opt-out posture,
  event names, and fields -- never left to drift from the code.
