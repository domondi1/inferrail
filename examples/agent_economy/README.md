# One job, one budget, everything it buys

> **Experimental example. Local and Base Sepolia testnet only.** Not part
> of the Inferrail gateway, not a product, no mainnet path, no real money.

| | Local mode (default) | `--testnet` |
|---|---|---|
| x402 seller middleware, 402 challenge, payment headers | real | real |
| EIP-3009 payment signatures, signature verification | real | real |
| Budget ledger, reservations, delegation, refusals | real | real |
| Chain, USDC balances, settlement | **simulated** (in-memory model of USDC's rules) | real (Base Sepolia, public x402.org facilitator) |
| Model provider | **stub** with operator-declared prices | **stub** |
| Seller's work (search, chat) | toy, deterministic | toy, deterministic |

If you want an agent to pay for things on its own (x402 search, a
pay-per-request LLM, another agent's API), the usual options are to give
it a funded key and hope, or approve every payment yourself. Wallet caps
help, but they're flat. They don't know which job a payment belongs to,
they don't cover the model calls you pay for with an API key, and they
don't split a budget between an agent and the sub-agents it spawns.

This example gives **one unit of work** one budget across all of it:

```
research-run-42, authority $0.020
  ├─ model call                       (provider key held by the authority runtime)
  ├─ x402 paid search                 (payer key held by the authority runtime)
  └─ sub-agent "summarizer", $0.006   (gets a token, never a key)
        ├─ x402 paid LLM call
        ├─ x402 paid search
        └─ x402 paid search  -> REFUSED before anything is signed
```

The agents are separate processes. Their environment holds an authority
URL and a token, nothing else. To spend, they ask the authority runtime,
which reserves the cost against their share of the budget and only then
signs the x402 payment. If the reservation fails, nothing is signed and
nothing is sent.

## Run it

```bash
git clone -b strategy/agent-economy-2026-10 https://github.com/domondi1/inferrail
cd inferrail
pip install "x402[evm,fastapi,httpx]==2.22.0" uvicorn
python examples/agent_economy/demo.py
```

About a minute from a fresh clone, including the install; the demo itself
takes two seconds. No keys, no account, no money. Output (abridged):

```
  [parent] model call                     -> cost $0.000251
  [parent] x402 paid search    $0.002     -> SETTLED, top hit: budgets
  [parent] delegated $0.006 to sub-agent 'summarizer' (token only, no key)
  [child ] x402 paid LLM call  $0.003     -> SETTLED
  [child ] x402 paid search    $0.002     -> SETTLED
  [child ] x402 paid search    $0.002     -> REFUSED before signing (remaining $0.001)
  [parent] 8 concurrent x402 searches against what's left of the budget:
  [parent]   settled=6 refused-before-signing=2

=== Economic record: research-run-42 ===
original authority      $0.02
delegated to summarizer  $0.006  spent $0.005  returned to parent $0.001
settled spend (total)   $0.019251
remaining authority     $0.000749
ledger invariant        SATISFIED
refused BEFORE any payment was authorized: 3
payer USDC outflow: $0.019 (= x402 settled spend; can never exceed the $0.02 funded)
```

Every settled x402 line carries its transaction hash and an on-chain
`authorizationState` check. Every refused line shows what was asked for
and what was left.

### On Base Sepolia (test USDC only)

```bash
PAYER_PRIVATE_KEY=0x...  SELLER_PAY_TO=0x...  python examples/agent_economy/demo.py --testnet
```

The payer is a Base Sepolia wallet holding a few cents of test USDC
([Circle faucet](https://faucet.circle.com)). Settlement goes through the
public `https://x402.org/facilitator`, and the runtime reconciles by
reading the USDC contract directly. There is no mainnet path in this
example.

## How the budget holds

1. **Reserve first.** Every economic action is a reservation against the
   caller's remaining authority, made in one SQLite `BEGIN IMMEDIATE`
   transaction (`hosted/a2a_economic_authority/core.py`). Two agents
   racing for the last dollar serialize; one wins and one is refused.
2. **Then sign, for exactly that.** The runtime signs an EIP-3009
   authorization bound to the seller's address, that amount, and a short
   validity window. The intent is recorded before the signature leaves the
   process.
3. **The chain decides what happened.** After the call, or after a crash,
   the runtime reads `authorizationState(payer, nonce)` from the token
   contract. If it's used, the spend is recorded with its transaction. If
   the window has passed unused, the authorization can never settle, so
   the reservation is released. Until then it stays held.
4. **The balance is the backstop.** Fund the payer account with the root
   budget. Even if this ledger were wrong, the account can't pay more
   than it holds.

## What is and isn't guaranteed

Proven by `tests/unit/test_agent_economy_example.py` (12 tests):

- An action that doesn't fit the caller's remaining authority is refused
  before any payment is signed or sent.
- 20 concurrent $0.002 purchases against $0.010 settle exactly 5 times;
  10 concurrent delegations of $0.002 from $0.010 grant exactly 5.
- A sub-agent looping 40 concurrent purchases never exceeds its delegation.
- Crash after reserving, crash after signing, or crash after settlement:
  on restart every action ends in exactly one state, a reservation is
  never released while its payment could still settle, and a settled
  payment is never left unrecorded.
- A tampered authorization is rejected (x402's own signature check).
- The payer key never appears in any status or record output.

Not guaranteed, stated plainly:

- **The runtime is trusted.** It holds the payer key. If this process or
  its key is compromised, the bound falls back to the payer account's
  balance. An on-chain cap (a smart-account delegation, ERC-7710) would
  remove that trust; x402 specifies it, but SDK support isn't available
  yet.
- **The seller isn't.** x402 `exact` has no refunds. A seller can take a
  payment and return garbage. The loss is bounded by that one reservation,
  but it isn't prevented.
- **Model-call cost is provider-reported**, priced from operator-declared
  rates, as in the gateway. The model provider here is a stub.
- **Local mode simulates the chain.** x402, the signatures, and the
  signature verification are real; the USDC ledger is an in-memory model
  of the contract's rules. Use `--testnet` for real settlement.
- Single runtime, one payer key, EVM `exact` payments in USDC only. No
  `upto`, refunds, discovery, or hosted mode.

## Files

| File | What it is |
|---|---|
| `authority.py` | The self-hosted authority runtime: reserve, sign, reconcile, record |
| `agent.py` | An agent process with a token and no key |
| `seller.py` | A stock x402 seller, with no Inferrail code in it |
| `local_chain.py` | Local stand-in for Base Sepolia USDC + an x402 facilitator over it |
| `demo.py` | Wires it together; `--testnet` for Base Sepolia |
