# 0019. An opt-in, anonymous usage ping

## Status

Accepted

## Context

Every "download it" claim in `MISSION.md` has, until now, had no
after-install signal at all — PyPI download counts and GitHub
clones/stars are free and already available, but they say nothing about
whether an install ever reaches first value (a tool connected, a
receipt, a budget). The founder explicitly wants this signal captured
before the HN launch, with hard, non-negotiable privacy constraints:
opt-in, default off, anonymous, lifecycle-only (never per-request),
fails silently, independently verifiable by anyone who doesn't want to
trust this document.

This sits directly against `MISSION.md`'s own non-negotiable — "no
telemetry without explicit opt-in (default off)" — and against
`telemetry/sinks.py`'s and `receipts/sinks.py`'s own docstring
guarantees that neither of those existing subsystems transmits data off
the machine. This feature is the one deliberate, narrow exception to
that whole-repo default, and it has to be architecturally impossible to
confuse with either of those existing local-only systems.

## Decision

**A new `inferrail.usage_ping` package, entirely separate from
`telemetry/` and `receipts/`.** It does not reuse `TelemetrySink` or
`ReceiptSink` as its transport — only a `receipt_hook.UsagePingReceiptSink`
*wraps* a `ReceiptSink` to observe when a receipt happens, without ever
reading a receipt's business-attribute fields.

**The exact payload (`usage_ping/payload.py`) is fixed and exhaustive:**
a locally-generated random `install_id` (not derived from any
machine/hardware identifier), `os`, `inferrail_version`, `event`, `ts`.
Four lifecycle events only: `first_run`, `tool_connected`,
`first_receipt`, `budget_created`. Nothing per-request, ever — never a
prompt, response, model name, cost, work_id, project name, or anything
about the traffic this install actually handles.

**Off by default, and inert with no endpoint configured, regardless of
the on/off toggle.** `UsagePingConfig.enabled` defaults to `False`;
`UsagePingConfig.endpoint` defaults to `None` and this package ships
with **no built-in default endpoint** — there is nothing for a fresh
install to ping even if a user (or a future release with a different
default) sets `enabled: true`, until an operator explicitly configures
`usage_ping.endpoint`. `usage_ping/client.py`'s `maybe_send_event` checks
`endpoint` before anything else, including before touching the local
state file — an unconfigured install does zero work per call, not just
zero network.

**A separate, mutable on/off state, not just the config file.** The
dashboard's Settings toggle and `inferrail telemetry enable|disable`
need to flip this live, from a process that may not be the one that
loaded `inferrail.yaml`. `usage_ping/state.py`'s `usage-ping-state.json`
(under the OS app-data dir, alongside `receipts.db`/`budgets.db`) is the
source of truth once it exists; the config file's `usage_ping.enabled`
only seeds its first value. `endpoint`, by contrast, is **never**
user-settable at runtime — it stays a config-file/operator decision.
Letting a dashboard visitor redirect anonymous pings to an arbitrary URL
would be a real abuse surface for no real benefit; the toggle only ever
turns sending on or off against whatever endpoint the operator chose.

**Each of the four events fires at most once per install, ever**, via
an idempotent local marker in the same state file
(`mark_event_sent_if_new`) — not a full lock (a plain read-modify-write),
so two processes racing at the exact same instant could in principle
both send once; the worst case is one duplicate anonymous event, never a
correctness or privacy problem.

**Sending never blocks, slows, or can fail the request path.**
`maybe_send_event` does its (cheap, local) check-and-mark synchronously,
then spawns a daemon thread for the actual `httpx.post` (3s timeout) —
the caller never waits on it, and any exception in that thread (offline,
DNS failure, timeout, non-2xx) is caught and logged at debug level only,
never raised or surfaced.

**Integration points, all only under `--app-mode`** (the toggle only
exists on the app-mode dashboard, so there is nothing to activate
without it — matching `budgets`/the local control API's own
`--app-mode`-only scoping):

- `first_run`: fired once from `create_app`'s own app-mode startup.
- `first_receipt`/`tool_connected`: fired from `UsagePingReceiptSink`,
  which wraps the `ReceiptSink` the two inference engines hold (not the
  raw `ReceiptsStore` the budget enforcer/local API still use directly —
  those need real `.query()`, which the wrapper deliberately doesn't
  implement). `tool_connected` only fires on a `status: "success"`
  receipt — the actual signal that some real client completed a request.
- `budget_created`: fired from `POST /v1/local/budgets` only, not from
  `inferrail budget set` — that CLI command is deliberately
  config-independent (works against a bare `--db` path with no
  `inferrail.yaml` at all), and requiring it to load a config just to
  check a ping setting would add exactly the coupling it was designed to
  avoid. The dashboard is the primary surface this milestone targets
  anyway.

**Independently verifiable, not just documented:** `inferrail telemetry
preview` prints the exact JSON body for every event, built from the raw
`install_id`/OS/version this install would actually use, without
sending anything — so nobody has to trust this ADR or
`docs/privacy/usage-ping.md`'s prose to know what the payload is.

**The receiver is proposed and built, but deployment is a human
action.** `hosted/usage_ping/service.py` is a small FastAPI receiver
(same pattern as `hosted/ap_exceptions`): one unauthenticated `POST
/ping` (the payload is harmless and anonymous, so an API key would only
add friction), request-size limit, per-IP rate limit, a kill switch, and
**no persistence of the connecting IP address** at all. This ADR
proposes self-hosting it on Render's free tier (the same platform
`hosted/ap_exceptions` already uses, so no new vendor account is
needed) over a third-party analytics service, since Inferrail's own
positioning is "your prompts never leave your machine's control" — routing
anonymous install pings through a third party sits awkwardly against
that even though the payload itself is harmless. See `PROGRESS.md`'s
"HUMAN ACTION NEEDED" for the exact deploy steps and what this session
could not do (create the Render service, choose the real URL).

## Consequences

- Until a real endpoint is deployed and `usage_ping.endpoint` is set,
  the feature is fully built and testable end-to-end (verified this
  session against a local mock collector — see `PROGRESS.md`) but
  produces zero real-world signal. The Settings screen states this
  plainly ("Not yet active — no collection endpoint is configured") so
  the toggle never looks like it works when it can't.
- Once a real endpoint exists, shipping it as the package's baked-in
  default (rather than requiring every user to hand-configure
  `usage_ping.endpoint`) is a separate, later decision — this ADR
  doesn't make it. Until then, only an operator who explicitly sets
  `usage_ping.endpoint` (e.g. pointed at their own deployment) can ever
  activate this on any install.
- `hosted/usage_ping/` is a new deployable, independent of
  `hosted/ap_exceptions/` and `hosted/a2a_economic_authority/` — its own
  process, its own storage, no shared code path, matching this
  project's existing rule that hosted surfaces stay isolated from each
  other and from the OSS data plane.
- The OSS data plane's own guarantee — "keeps working with zero
  dependency on any Inferrail-operated service" — is unaffected: the
  gateway, receipts, and budgets all function identically whether or not
  `usage_ping` is configured, enabled, or reachable.
