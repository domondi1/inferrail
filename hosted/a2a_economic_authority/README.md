# Inferrail Economic Authority (hosted) — Phase B

**Status: durable core (Phase A) plus authenticated A2A transport (Phase
B), security-repaired.** `core.py` is the transport-independent durable
economic-authority core: reserve/grant/consume/settle over a delegated
spending ceiling, with conservation and idempotency guarantees enforced by
SQLite. `executor.py`, `agent_card.py`, `access_control.py`, and
`server.py` add a real, locked-down A2A server on top of it, and
`capabilities.py` adds a capability-token authorization layer.

**Not yet present, by design at this stage:** any payment/x402 wiring, any
deployment configuration, and any recursive/automatic delegation between
agents -- every operation is a direct call initiated by a caller. The
`authority_usd` ceiling tracked here is caller-declared accounting/policy
metadata: Inferrail does not hold, transfer, or escrow the underlying
money. Whether and how payment is added later is a separate, not-yet-made
decision; nothing here should be read as committing to a specific future
mechanism or timeline.

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
credential happened to trigger the reservation. This is also what
separates `grant` authority from `reserve` authority: a claim is bound to
"currently holds `reserve` scope on this `parent_id`", so a credential
that holds only `grant` can unblock a parked reservation (by supplying the
missing authority) but can never itself redeem, resume, or hijack that
reservation's resulting child credential. An outstanding, unclaimed claim
is purged immediately when its issuing delegation's tree is revoked, and
concurrent redemption attempts for the same claim are serialized so
exactly one can ever succeed.

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
race-safety) would remain correct. There is no deployment configuration
in this repository yet that could introduce a multi-worker setup, but this
constraint is enforced by omission today and must be addressed explicitly
before any future phase adds one.

## CI

`hosted/a2a_economic_authority/`'s tests are only meaningfully exercised
by the `hosted-economic-authority` job in `.github/workflows/ci.yml`,
which installs the `hosted` extra (so the real `a2a-sdk` transport tests
run instead of self-skipping, the same way `hosted/work_economics/`'s
cdp-sdk/x402 tests already do in the main `test` job) and runs `mypy
hosted/a2a_economic_authority --strict` directly, since this directory is
deliberately excluded from the main `mypy` invocation's package list (see
`docs/adr/0010`).

## Known limitations (Phase B)

- No payment of any kind. There is no public endpoint that creates a root
  delegation or its capability -- the only way either comes into existence
  today is `bootstrap.py`, called directly in a test process, never over
  HTTP. Whether and how a paid path is added later is undecided.
- No recursive/automatic delegation: every operation is a direct call
  initiated by a caller. Nothing in this service ever calls another agent.
- `agent_id` is a caller-supplied label, not a verified identity.
- No deployment configuration exists yet; when one is added, it must run
  this service as a single process (see above) and terminate HTTPS in
  front of it.
