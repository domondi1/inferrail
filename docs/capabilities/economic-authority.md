# Inferrail Economic Authority (working name)

**Status:** Base Sepolia testnet only. Not mainnet, not real money, and
not deployed anywhere yet — unlike Work Economics, there is no hosted
instance of this capability to call. Run
`hosted/a2a_economic_authority/server.py` yourself to try it (see its
`--help`).

Inferrail's second hosted, paid capability. An agent purchases a durable
**Economic Authority session**: a coordination boundary that lets
multiple agents operate under a shared, buyer-declared spending ceiling
without double-allocating that authority. Payment is the real
[x402](https://www.x402.org/) protocol — any x402-capable agent can buy
it with its own wallet, no Inferrail account required. Every operation on
a purchased session runs over [A2A](https://a2a-protocol.org/) JSON-RPC
transport, not plain REST.

## What you are buying

The x402 fee (`service_fee`) pays **only** for Inferrail creating and
hosting the coordination boundary itself. It is never a deposit into, or
escrow of, the ceiling you declare:

| Field | What it is | Who holds it |
|---|---|---|
| `service_fee` | The fixed testnet fee paid to Inferrail for opening the session (currently $0.05) | Paid to Inferrail, real (testnet) on-chain transfer |
| `authority_ceiling_usd` | Your own declared spending ceiling for this coordination boundary | **You.** Caller-declared accounting/policy metadata enforced entirely in Inferrail's bookkeeping — Inferrail never funds, holds, or moves this amount |

These two numbers are never conflated in any response shape, and never
will be — see [`response.schema.json`](schemas/economic-authority/response.schema.json).

## Endpoints

```
GET  <base_url>/.well-known/agent-card.json   # A2A Agent Card, no payment required
POST <base_url>/                              # A2A JSON-RPC (SendMessage): status, reserve, grant, consume, settle, revoke
POST <base_url>/sessions                      # purchase a session (x402-gated)
POST <base_url>/sessions/recover              # recover a session's root credential (unpaid)
POST <base_url>/capabilities/claim            # claim a freshly minted child credential (plain HTTP, outside A2A)
```

## 1. Discover the Agent Card

`GET /.well-known/agent-card.json` declares the service's name, its six
skills (`reserve`, `grant`, `consume`, `settle`, `status`, `revoke`), the
JSON-RPC transport endpoint, and its bearer security scheme
(`capabilityBearer`). No payment or prior knowledge required to read it.

## 2–5. Purchase a session

`POST /sessions`, schema: [`request.schema.json`](schemas/economic-authority/request.schema.json) →
[`response.schema.json`](schemas/economic-authority/response.schema.json).

```json
{
  "agent_id": "string",
  "authority_ceiling_usd": "decimal string, e.g. \"10.00\"",
  "recovery_secret_hash": "64-char lowercase hex SHA-256 digest"
}
```

`recovery_secret_hash` is **required**, not optional: it is the
hex-encoded SHA-256 of a high-entropy secret you generate and keep for
yourself, client-side, **before** ever sending this request. A request
missing it (or with a malformed value) is rejected with `400` **before
any x402 processing is attempted** — you are never charged for a purchase
that could not honestly promise recovery.

An unpaid call returns **HTTP 402** with x402 `PaymentRequirements`
(currently `$0.05` on Base Sepolia, `eip155:84532`). Retry the identical
request with the signed `PAYMENT-SIGNATURE` header the 402 asked for.
Settlement happens **before** this route's handler ever runs (x402's
`"upfront"` payment flow) — a settlement failure returns 402 directly,
and there is no code path where a `200` response is returned dishonestly.

```json
{
  "session_id": "server-generated identifier",
  "agent_id": "string",
  "authority_ceiling_usd": "10",
  "service_fee": {"amount_usd": "0.05", "currency": "USD", "status": "PAID"},
  "root_capability": {
    "token": "opaque bearer token — shown exactly once",
    "scopes": ["consume", "grant", "read", "reserve", "revoke", "settle"],
    "instructions": "..."
  },
  "newly_claimed": true
}
```

The root token is shown **exactly once**, in this response. It is never
persisted anywhere in plaintext by Inferrail — only its SHA-256 hash.

## 6. Operate the session (A2A JSON-RPC)

Every operation is a `SendMessage` call carrying one JSON data part with
an `op` field, authenticated by `Authorization: Bearer <token>` (never
inside the message itself). A successful op returns a `Task` in
`TASK_STATE_COMPLETED` with a data-part artifact. For every op except
`reserve`, that artifact is a `receipt` — schema:
[`receipt.schema.json`](schemas/economic-authority/receipt.schema.json).
`reserve`'s artifact has a different shape (`credential_claim_id`, not a
receipt) — see below. A failure returns
`TASK_STATE_FAILED`/`TASK_STATE_REJECTED` with an `error` data-part —
schema: [`error.schema.json`](schemas/economic-authority/error.schema.json).

| `op` | Required fields | Scope required |
|---|---|---|
| `status` | `delegation_id` | `read` |
| `reserve` | `event_id`, `parent_id`, `delegation_id`, `agent_id`, `maximum_usd` (optional: `child_scopes`) | `reserve` on `parent_id` |
| `grant` | `delegation_id`, `event_id`, `amount_usd` | `grant` on `delegation_id` |
| `consume` | `delegation_id`, `event_id`, `amount_usd` (may be `null` for unknown cost) | `consume` on `delegation_id` |
| `settle` | `delegation_id`, `event_id`, `outcome` | `settle` on `delegation_id` |
| `revoke` | `delegation_id`, `event_id` | `revoke` on `delegation_id` or an ancestor of it |

`reserve` never hands back the new child's credential directly — the A2A
transport has no channel to deliver one outside task history, which must
never carry a credential. It returns a `credential_claim_id` instead;
retrieve the real token with a **separate** call:

```
POST /capabilities/claim  {"claim_id": "..."}
Authorization: Bearer <the EXACT token that authorized the reserve>
```

If the parent's remaining headroom is insufficient, `reserve` parks the
task at `TASK_STATE_AUTH_REQUIRED` instead of failing; a `grant` on the
same `task_id` supplies the shortfall and the reservation retries
automatically.

`revoke` tears down the delegation **and its entire descendant subtree**:
every active descendant is settled from the leaves up, and every
capability token scoped to any of them is revoked in the same step.

## 7. Recover a lost credential

`POST /sessions/recover`, unpaid — schema:
[`recovery.schema.json`](schemas/economic-authority/recovery.schema.json).

```json
{"payment_nonce": "the EIP-3009 nonce from your original payment", "recovery_secret": "the plaintext secret you generated in step 2"}
```

Deliberately keyed on `payment_nonce`, **never** `session_id`. Your own
x402 client generates `payment_nonce` before it ever signs or sends the
payment (see `x402.mechanisms.evm.utils.create_nonce`), so you already
hold it independent of any response from this service — unlike
`session_id`, which you can only learn from the very response you might
have lost. `payment_nonce` alone is never sufficient authorization (it is
not secret); presenting the matching `recovery_secret` is what proves
ownership.

```json
{
  "session_id": "resolved for you, since you may not know it",
  "root_capability": {"token": "a FRESH bearer token", "scopes": [...], "instructions": "..."}
}
```

Recovering also transparently completes a purchase that settled but was
interrupted before being fully materialized (see Limitations) — not only
one that fully completed and was merely lost in transit. Every failure
mode (unknown `payment_nonce`, no recovery opt-in, or a wrong secret)
returns the same `403 {"error": "InvalidRecoverySecret"}`, so this route
can never be used to enumerate valid payment nonces.

## Price

Currently **$0.05 USD** per session, quoted and settled in test-USDC on
Base Sepolia (configurable server-side via
`ECONOMIC_AUTHORITY_SESSION_PRICE_USD`). One price per session purchase —
every subsequent operation on that session (`status`, `reserve`, `grant`,
`consume`, `settle`, `revoke`) is unpaid.

## Buyer requirements

Any x402-capable agent with a plain EVM private key (e.g. `x402`'s
`EthAccountSigner`), plus an `a2a-sdk` client for the operations above —
no CDP account, no Inferrail account, no prior relationship with
Inferrail. See
[`examples/economic_authority_session.py`](../../examples/economic_authority_session.py)
for a complete, standalone client covering discovery through recovery.

## Limitations

- **Base Sepolia testnet only.** No mainnet.
- **No custody or escrow.** Inferrail does not fund, hold, or move the
  declared `authority_ceiling_usd`.
- `agent_id` is a caller-supplied label, not a verified identity.
- Supplier/consumption costs recorded via `consume` are caller-reported,
  not independently verified by Inferrail.
- No automatic recursive downstream execution — every operation is a
  direct call initiated by a caller; nothing in this service ever calls
  another agent on its own.
- No guarantee of exactly-once downstream execution beyond this
  service's own exactly-once economic bookkeeping (reservation, consume,
  settle) — what a *worker* does with a reserved budget is outside this
  capability's guarantee.
- **Single-process requirement.** Task state and reservation-credential
  handoff are process-local (not durable); this service must run as a
  single process. Economic and capability state (SQLite) survive a
  restart; an unclaimed reservation credential or a parked task does not.
- **A settlement-succeeds-but-crashes-before-any-durable-write window has
  no automatic recovery.** If the process dies after x402 settlement
  succeeds but before the first durable purchase record is written, there
  is nothing yet for `/sessions/recover` to key off, and this cannot be
  closed using the installed x402 SDK's facilitator client alone (it
  exposes no reconciliation query). This is accepted only for this
  experimental, testnet-only phase and is a **blocking prerequisite** for
  any real (mainnet) deployment — it must be closed with a reconciliation
  or refund/support mechanism first. Every other point between settlement
  and a fully materialized session (a lost response; a crash before the
  root or its credential was created) already recovers automatically via
  `/sessions/recover`.

Every claim above is backed by a passing test — see
`tests/unit/hosted/test_a2a_economic_authority_sessions.py`,
`test_a2a_economic_authority_payment_settlement_boundary.py`,
`test_a2a_economic_authority_transport.py`, and
`test_a2a_economic_authority_public_schemas.py`, which validates the
schemas linked above against real request/response bodies.
