# Inferrail Economic Authority (hosted)

**Status: durable core (Phase A), authenticated A2A transport (Phase B),
x402-gated paid session creation (Phase C), the public contract/ADR/
schemas/example (Phase D), and deployment-readiness (Phase E prep), all
security-repaired.** `core.py` is the transport-independent durable
economic-authority core: reserve/grant/consume/settle over a delegated
spending ceiling, with conservation and idempotency guarantees enforced
by SQLite. `executor.py`, `agent_card.py`, `access_control.py`, and
`server.py` add a real, locked-down A2A server on top of it,
`capabilities.py` adds a capability-token authorization layer, and
`sessions.py` adds the Phase C paid-session business logic behind
`POST /sessions` (and its unpaid recovery sibling, `POST
/sessions/recover`) -- see "Phase C: paid session creation" below. See
[`docs/capabilities/economic-authority.md`](../../docs/capabilities/economic-authority.md)
for the public, agent-facing contract.

**Not yet deployed anywhere.** `server.py` now has what a deployment
needs -- a `/health` liveness route and an environment-variable-driven
production startup shape (see "Deploying it" below) -- but no instance of
this service is running on any host. **Still not present, by design:**
any recursive/automatic delegation between agents -- every operation is
a direct call initiated by a caller. The `authority_ceiling_usd` tracked
here is caller-declared accounting/policy metadata: Inferrail does not
hold, transfer, or escrow the underlying money, even now that a real
x402 payment exists -- that payment is Inferrail's service fee for
creating and hosting the coordination boundary, never a deposit into, or
escrow of, the ceiling itself. See "Phase C" below for the full design.

## What's here

- `core.py` — durable economic state (`EconomicAuthorityStore`), including
  the per-`(delegation_id, event_id)` idempotency/conflict tracking and
  the durable revocation-in-progress marker described below.
- `capabilities.py` — capability-token authorization (`CapabilityStore`,
  `InMemoryCredentialHandoff`). High-entropy opaque bearer tokens; only
  hashes are ever persisted.
- `executor.py` — the A2A `AgentExecutor` dispatching reserve/grant/
  consume/settle/status/revoke, plus a hardened `cancel()`.
- `agent_card.py` — the public Agent Card, including the HTTP Bearer
  security scheme. Declaring this scheme documents the requirement; it
  does not by itself enforce anything -- see `access_control.py`.
- `access_control.py` — locks the reachable A2A surface down to
  `SendMessage` only. Declaring a bearer security scheme on the Agent Card
  does not, by itself, stop an unauthenticated caller from reading,
  enumerating, cancelling, or subscribing to another agent's task through
  the SDK's other standard methods (`GetTask`, `ListTasks`, `CancelTask`,
  `SubscribeToTask`, push-notification config, `GetExtendedAgentCard`) --
  this module disables all of them explicitly.
- `server.py` — FastAPI/A2A server assembly, plus the one deliberate
  non-A2A route (`POST /capabilities/claim`) documented in its module
  docstring.
- `bootstrap.py` — test-only root-delegation/root-capability bootstrap.
  Never wired to an HTTP route; there is no unauthenticated public
  root-creation endpoint.
- `requirements.txt` — pinned runtime dependencies for running this
  service locally (`pip install -e ".[hosted]"` also installs these; see
  the `hosted` extra in the repo's `pyproject.toml`).

Like `hosted/work_economics/`, this directory is not part of the
`inferrail` package: it is not imported by any gateway code path, is not
included in the wheel build (see `pyproject.toml`'s
`[tool.hatch.build.targets.wheel]` package list), and `inferrail serve`
has no dependency on it. See `docs/adr/0004-data-plane-control-plane-boundary.md`
for the general principle this follows.

## Authorization design

Six scopes: `read`, `reserve`, `grant`, `consume`, `settle`, `revoke`.
Every capability token is bound to exactly one `delegation_id` and one
scope set. A `revoke`-scoped token additionally authorizes revoking any
*descendant* of its own delegation (not just an exact match) -- this is
what lets a root owner revoke its complete delegation tree; every other
operation requires an exact `delegation_id` match.

Only `SendMessage` is reachable at the A2A transport layer
(`access_control.py`); every other standard A2A method is explicitly
disabled, regardless of whether the caller presents a valid, invalid, or
missing credential. `EconomicAuthorityExecutor.cancel()` is additionally
hardened to refuse an unauthenticated or unrelated-credential cancellation
even though it is currently unreachable via HTTP, as defense-in-depth.

**Credential channel.** Presented credentials always travel in the
standard `Authorization: Bearer <token>` HTTP header -- for every A2A
`SendMessage` call and for the claim route below -- never inside message
content, extension metadata, task history, or an economic receipt. When
this service is deployed (it is not yet -- see "Known limitations"), every
one of these HTTP calls, including the claim route, **must** run over
HTTPS; nothing about this design is safe over an unencrypted connection.

**Newly minted child credentials and the claim route.** `reserve` is the
only operation that mints a brand-new credential. The installed A2A SDK
gives an `AgentExecutor` no channel to return data outside persisted A2A
Task/Message content (see `server.py`'s module docstring for the exact
SDK code path), so the credential itself is never placed in a `reserve`
response. Instead, `reserve` returns a non-secret, single-use
`credential_claim_id`, and the actual plaintext token is returned **once**,
in the JSON body of an authenticated `POST /capabilities/claim` call --
outside the A2A pipeline entirely. That response is marked `Cache-Control:
no-store` (plus `Pragma: no-cache`) so no intermediary caches it.

At claim time, the presented bearer credential is **fully revalidated**
against the live `CapabilityStore` -- existence, expiry, revocation,
delegation binding, and scope -- not merely hash-matched against whichever
credential happened to trigger the reservation. Redemption is bound to the
**exact credential that originally authorized the reservation**, by its
non-secret `token_id` (`authorizing_token_id`), not merely to "any
credential that currently holds `reserve` scope on this `parent_id`". A
different, otherwise-valid `reserve`-scoped credential presented at claim
time is rejected (`WrongAuthorizer`), even if it is live and correctly
scoped -- it simply is not the credential that made this particular
reservation. This is also what separates `grant` authority from `reserve`
authority: a credential that holds only `grant` can unblock a parked
reservation (by supplying the missing authority) but can never itself
redeem, resume, or hijack that reservation's resulting child credential.
An outstanding, unclaimed claim is purged immediately when either its
issuing delegation's tree **or the child delegation it targets** is
revoked, and concurrent redemption attempts for the same claim are
serialized so exactly one can ever succeed.

**Crash-safe recovery vs. ordinary retries.** Committing the economic
reservation, minting the child capability, and creating its claim happen
as separate steps across two SQLite databases and one in-memory buffer --
a crash or a lost response between any of them must not strand a real
reservation with no way to ever obtain its credential. But an ORDINARY
duplicate delivery of the same `reserve` request (the overwhelming common
case -- e.g. a network layer retrying a call whose first response
actually arrived fine) must never mint another credential or touch
whatever credential the caller already claimed and may be actively using.
These two situations are handled differently on purpose.

Before the economic reservation is even committed,
`capabilities.record_reservation_authorization` durably binds the
child's `delegation_id` to the exact authorizing credential's `token_id`
and the requested `child_scopes`, in `capabilities.sqlite3`. A matching
retry of the same `reserve` request is, by default, a pure no-op: it
reports the existing reservation and never mints or rotates anything,
regardless of who sends it. Recovery only happens when the caller
explicitly sets `recover_credential: true` in the `reserve` payload --
sent specifically because the caller knows delivery of the credential was
genuinely lost (a crash, a timeout with no response), never as a side
effect of an ordinary retry. Only then does `rotate_reservation_credential`
mint a fresh child credential, revoking whatever one may already exist
for that delegation in the same atomic step, and hand back a brand-new
claim -- and only for the EXACT original authorizer: a different
credential, even one that legitimately holds `reserve` scope on the same
parent, cannot recover, rotate, claim, or mint access this way merely by
knowing or guessing the child's `delegation_id` and asking for recovery --
recovery checks the exact `token_id`, not scope or delegation match. The
underlying economic reservation is never re-created either way
(`core.reserve` is idempotent on `delegation_id` regardless of how many
times a retry or a recovery request runs), and only non-secret `token_id`s
are ever persisted -- never a plaintext credential.

**Idempotency boundary.** Economic event IDs are scoped to
`(delegation_id, event_id)`, never to `event_id` alone -- two unrelated
delegations choosing the same caller-picked event_id never collide.
`reserve`'s natural idempotency key is the *child* `delegation_id` itself.
Every mutating call stores a canonical JSON payload of its meaningful
fields alongside the event; replaying the same key with the same payload
is a safe no-op, but reusing a key with a materially different payload
(different amount, parent, agent, or outcome) raises `core.EventConflict`
-- surfaced to A2A callers as an explicit `TASK_STATE_FAILED`, never a
silent success or a silently-lost operation.

**Revocation race safety.** `core.mark_revocation_started` durably marks a
delegation as being torn down, as the first step of any revoke, before
anything reads which descendants currently exist. `core.reserve` checks
this flag across the entire ancestor chain of the requested parent, inside
its own atomic SQLite transaction, before creating anything. Because
SQLite's `BEGIN IMMEDIATE` serializes all writers against one database
file, there is no interleaving in which a new descendant can be created
after the mark, and none in which a reservation that committed before the
mark is invisible to the subtree scan performed after it -- closing the
snapshot-then-settle race a naive tree revocation would otherwise have.
See `core.py`'s module docstring for the full argument, and
`test_a2a_economic_authority_core.py`'s
`test_concurrent_reserve_and_revocation_mark_never_lets_a_reservation_escape_unmarked`
for the whitebox proof.

## Phase C: paid session creation

`POST /sessions` is the only x402-gated route in this service. A buyer
pays a fixed service fee (`ECONOMIC_AUTHORITY_SESSION_PRICE_USD`, default
$0.05, Base Sepolia test-USDC) to have this service create an **Economic
Authority session**: a durable root delegation with a buyer-declared
`authority_ceiling_usd`, plus its root capability token. `/sessions` (and
`/sessions/recover`) are registered only when
`ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS` is set; every Phase B
deployment/test that does not set it is unaffected.

**Settlement-before-handler.** The route uses x402's officially supported
`"upfront"` payment flow (`PaymentOption(..., extra={"paymentFlow":
"upfront"})`, supported by the `eip3009` asset-transfer method the `exact`
scheme uses here) instead of the scheme's default `"authorization"` flow.
Concretely: real on-chain settlement completes *before* this service's own
route handler ever runs, and a settlement failure returns a 402 straight
from the x402 middleware without ever calling the handler. This closes a
payment-security defect present in an earlier version of this route: under
the default flow, settlement runs *after* the handler, so the handler
would durably create a session, mint its root credential, and label the
service fee `"PAID"` before knowing whether settlement would actually
succeed -- a settlement failure after that point left an orphaned, unpaid
session and an unclaimed root credential behind, even though the
buyer-visible response was an honest failure. Under `"upfront"`, by the
time any code in `sessions.py` runs, the payment has already, genuinely
settled; there is no longer a code path where this service reports `PAID`
and settlement then fails. See `sessions.py`'s module docstring
("Settlement-before-handler") and
`tests/unit/hosted/test_a2a_economic_authority_payment_settlement_boundary.py`
for a deterministic reproduction of the historical defect alongside the
tests proving the fix.

**Idempotency.** The verified, on-chain EIP-3009 payment nonce
(`payment_nonce`) is the sole authoritative idempotency key: a given
nonce can only ever back one session, `session_id` is always
server-generated, and at most one root credential per session is ever
live -- see `sessions.py` and `capabilities.record_session_purchase`/
`issue_or_rotate_session_credential`. The route also accepts x402's
official payment-identifier extension (optional, not required); the
buyer-supplied `id` is recorded for audit/correlation only and is
deliberately never used as a substitute idempotency key, since (unlike
the verified nonce) it is buyer-chosen and not cryptographically bound to
the actual transfer -- see `sessions.py`'s module docstring for why
trusting it that way would risk a buyer's own reused `id` silently
absorbing a second, real payment.

**Recovery.** A buyer who was genuinely charged but never received (or
has since lost) their session's root credential can recover it via
`POST /sessions/recover` -- a plain, unpaid HTTP route (recovering access
to something already paid for costs nothing further). The request
identifies the purchase by `payment_nonce`, never the server-generated
`session_id`: `session_id` is minted by `record_session_purchase`
(`secrets.token_hex(16)`) and the buyer's only way to learn it is a
response from this service, so keying recovery on it would make recovery
unreachable in exactly the scenario it exists for (every response for
this payment lost). `payment_nonce`, by contrast, is generated by the
buyer's own x402 client *before* it ever signs or sends the payment (see
`x402.mechanisms.evm.utils.create_nonce` in the installed x402 2.22.0
SDK), so the buyer already holds it independent of any response.

`recovery_secret_hash` is now **required** (not optional) on every
`POST /sessions` request -- a SHA-256 commitment, computed client-side
over a secret the buyer generates and keeps for themselves, never sent to
us as plaintext on the happy path. It is enforced by its own middleware
that runs *before* the x402 payment middleware (see
`server._require_recovery_secret_hash`), specifically because under this
route's `"upfront"` flow settlement happens before the route handler
regardless of what the handler would validate -- a handler-level check
alone would reject an agent-first buyer that omitted the field only
*after* charging them. Because the buyer holds both `payment_nonce` and
the recovery secret locally from before the request was ever built,
recovery works even if every response this service ever sent for that
payment was lost. Recovery also re-runs the full idempotent creation
pipeline (`core.create_root`, then credential issuance/rotation), so it
transparently completes a purchase that settled but crashed before being
fully materialized, not only one that fully completed and was merely lost
in transit. See `sessions.recover_session` and
`capabilities.CapabilityStore.get_session_purchase_for_recovery`/
`rotate_session_credential`'s docstrings for the full guarantee,
including why `payment_nonce` alone (it is not secret) is never
sufficient authorization, and why an unrelated caller who does not hold
the real recovery secret can never use this route to obtain or rotate a
session's root credential.

**Known residual gap.** Recovery cannot help a payment that settled but
crashed before *any* durable `session_purchases` row was ever written --
there is nothing yet to recover. This is not closeable using only the
installed x402 SDK's facilitator client (`verify`/`settle`/`get_supported`
only, no reconciliation query) without either trusting undocumented
facilitator error-reason semantics or building direct on-chain
reconciliation, both out of scope for Phase C's smallest-architecture
mandate -- see `sessions.py`'s module docstring ("Known residual gap") for
the full analysis and why it must be resolved operationally, not silently
covered, before any real (mainnet) deployment.

## Durability and the single-process requirement

- **Durable (SQLite, `BEGIN IMMEDIATE`, survives process restart):**
  economic state (`core.EconomicAuthorityStore`) and capability-token
  issuance/revocation, including the revocation-in-progress marker above
  (`capabilities.CapabilityStore`). Both are also safe under multiple
  processes sharing the same database files -- SQLite's file-level locking
  serializes writers regardless of process boundary.
- **Not durable (in-process memory only, lost on restart):** A2A task
  state, including a `reserve` parked at `TASK_STATE_AUTH_REQUIRED`
  awaiting a `grant` (`a2a.server.tasks.InMemoryTaskStore`), and any
  unclaimed reservation credential
  (`capabilities.InMemoryCredentialHandoff`). A restart loses a parked
  task -- safe, since `core.reserve()` was never called for it, so there
  is nothing to reconcile, but the caller must re-issue the `reserve` from
  scratch. A restart also loses any *unclaimed* claim; the reservation
  itself is unaffected, only the as-yet-unclaimed credential is gone, same
  as an unclaimed one-time code from any other system would be.

Because of the second point, **this server must run as a single process**.
`server.py`'s `main()` never exposes a `--workers` option and calls
`uvicorn.run()` without one. Running multiple worker processes against the
same task/claim state would silently break both in-memory stores; only the
SQLite-backed economic and capability state (including revocation
race-safety) would remain correct. This constraint is enforced by
omission (no `--workers` flag exists to misuse -- see
`test_server_module_never_exposes_a_workers_flag`) and must be addressed
explicitly, not merely re-checked, before any future phase makes this
service multi-process.

## Deploying it

**Not deployed anywhere yet.** This section documents what a deployment
needs, verified locally, not a live instance.

Any host that can run a long-lived Python HTTPS **single** process (see
above) with a **persistent, private disk** for two SQLite files works.

Build step: `pip install -r requirements.txt` from this directory --
that alone is sufficient (it now includes `cdp-sdk` and `x402`, both
required unconditionally by `server.py`'s Phase C wiring, not just when
a session-purchase env var is set; see
`test_requirements_txt_installs_a_working_server.py`, which proves this
file alone lets the server import and reach its fail-closed startup
behavior in a fully isolated environment -- no extra packages need to be
installed alongside it).

Set:

- `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET` -- CDP facilitator credentials
  (secret). Only required if `ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS`
  is also set (Phase C's `/sessions` route).
- `ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS` -- the EVM address to
  receive the session service fee (not secret; a public on-chain
  address). Deliberately distinct from `hosted/work_economics`'s own
  `X402_SELLER_PAY_TO_ADDRESS` so the two capabilities' commercial
  identities never overlap. Omit this to run in Phase B mode only (no
  `/sessions` route at all).
- `ECONOMIC_AUTHORITY_SESSION_PRICE_USD` -- optional, default `0.05`
  (not secret).
- `ECONOMIC_AUTHORITY_SESSION_RESOURCE_URL` -- optional, defaults to
  `<base_url>/sessions` (not secret).
- `ECONOMIC_AUTHORITY_BASE_URL` -- **required** in the production shape
  below: the real public HTTPS URL this service is reachable at (not
  secret). Used for the Agent Card's own `url` field and, unless
  `ECONOMIC_AUTHORITY_SESSION_RESOURCE_URL` overrides it, the x402
  resource URL. `main()` refuses to start without it in this shape --
  see `test_fails_closed_in_production_shape_without_base_url`.
- `ECONOMIC_AUTHORITY_DB_PATH`, `ECONOMIC_AUTHORITY_CAPABILITY_DB_PATH` --
  **required** in the production shape below: file paths on the
  persistent disk for the two durable SQLite databases (not secret, but
  the files they point at must be on private storage -- see "Durable" vs.
  "Not durable" above for exactly what each file does and does not
  survive a restart).
- `PORT` -- injected by most hosting platforms (not secret).

Then run `python3 server.py` (no CLI args -- this is the production
shape: binds `0.0.0.0`, reads `$PORT`, and reads three of the variables
above directly -- `ECONOMIC_AUTHORITY_DB_PATH`,
`ECONOMIC_AUTHORITY_CAPABILITY_DB_PATH`, `ECONOMIC_AUTHORITY_BASE_URL` --
all three required in this shape). The other three -- `CDP_API_KEY_ID`/
`CDP_API_KEY_SECRET`/`ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS` and its
two optional siblings -- are read separately, at import time, by the
Phase C session-purchase wiring (`_wire_session_purchase_route`), not by
`main()` itself, and apply identically in either invocation shape.
Passing explicit `--port` `--db-path` `--capability-db-path` (optionally
`--host`/`--base-url`) is the local/test shape instead (binds
`127.0.0.1` by default); every existing test in this directory uses that
shape unchanged.

`GET /health` is a plain, unauthenticated liveness route for a platform's
health-check probe -- see `test_health_endpoint_returns_ok`.

Whatever host runs this must terminate HTTPS in front of it --
`/sessions`' plaintext root credential and `/sessions/recover`'s
plaintext recovery secret are exactly as HTTPS-dependent as every other
bearer credential this service issues.

## CI

`hosted/a2a_economic_authority/`'s tests are only meaningfully exercised
by the `hosted-economic-authority` job in `.github/workflows/ci.yml`,
which installs the `hosted` extra (so the real `a2a-sdk` transport tests
run instead of self-skipping, the same way `hosted/work_economics/`'s
cdp-sdk/x402 tests already do in the main `test` job) and runs `mypy
hosted/a2a_economic_authority --strict` directly, since this directory is
deliberately excluded from the main `mypy` invocation's package list (see
`docs/adr/0010`).

## Known limitations (Phase C)

- No mainnet, no custody, no escrow -- `/sessions` is Base Sepolia
  test-USDC only, and Inferrail never holds, transfers, or escrows
  `authority_ceiling_usd`.
- No recursive/automatic delegation: every operation is a direct call
  initiated by a caller. Nothing in this service ever calls another agent.
- `agent_id` is a caller-supplied label, not a verified identity.
- A payment that settles but crashes before any `session_purchases` row
  is ever durably written has no automated recovery path (see "Known
  residual gap" above) -- this must be resolved with a human-support/
  refund runbook before any real (mainnet) deployment.
- Not deployed anywhere yet -- see "Deploying it" above for what a
  deployment needs and what has been verified locally in advance of one.
- `/sessions`' payment-settled-but-crashed-before-first-durable-write
  residual gap (previous bullet) means a real deployment needs a
  human-support/refund runbook in place from the start, not added later.
