# Inferrail Economic Authority (hosted) — Phase B

**Status: durable core (Phase A) plus authenticated A2A transport (Phase
B).** `core.py` is the transport-independent durable economic-authority
core: reserve/grant/consume/settle over a delegated spending ceiling, with
conservation and idempotency guarantees enforced by SQLite. `executor.py`,
`agent_card.py`, and `server.py` add a real A2A server on top of it, and
`capabilities.py` adds a capability-token authorization layer.

**Not yet present, by design at this stage:** any payment/x402 wiring
(Phase C adds a paid `POST /sessions` path), any deployment configuration,
and any recursive/automatic delegation between agents -- every operation is
a direct call initiated by a caller.

## What's here

- `core.py` — durable economic state (`EconomicAuthorityStore`). Unchanged
  from Phase A.
- `capabilities.py` — capability-token authorization (`CapabilityStore`,
  `InMemoryCredentialHandoff`). High-entropy opaque bearer tokens; only
  hashes are ever persisted.
- `executor.py` — the A2A `AgentExecutor` dispatching reserve/grant/
  consume/settle/status/revoke.
- `agent_card.py` — the public Agent Card, including the HTTP Bearer
  security scheme.
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
what lets a root owner revoke its complete delegation tree, per the
approved requirements; every other operation requires an exact
`delegation_id` match.

Credentials travel only via the real HTTP `Authorization: Bearer <token>`
header -- never inside A2A message content, extension metadata, task
history, or economic receipts. See `server.py`'s module docstring for the
one documented exception this requires: handing back a *newly minted*
child capability (from `reserve`) needs a small, deliberate side-channel
(`POST /capabilities/claim`) because the installed A2A SDK gives an
`AgentExecutor` no channel to influence the outbound HTTP response other
than persisted Task/Message content.

## Known limitations (Phase B)

- No payment: Phase C adds `POST /sessions` over x402. Until then, the
  only way a root delegation and its capability come into existence is
  `bootstrap.py`, called directly in a test process -- never over HTTP.
- No recursive/automatic delegation: every operation is a direct call
  initiated by a caller. Nothing in this service ever calls another agent.
- `agent_id` is a caller-supplied label, not a verified identity.
- The unclaimed-credential handoff (`InMemoryCredentialHandoff`) lives only
  in server process memory and does not survive a restart; an unclaimed
  `reserve` claim is lost on restart (the reservation's economic effect is
  not -- that lives in `core.py`'s durable store).
