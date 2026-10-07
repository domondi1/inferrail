# Hierarchical budget provider (TypeScript reference)

A small reference for spend-policy hooks that need **sub-agent
budgets**: for example, a pre-execution `policyProvider` in front of x402 or
wallet actions. The provider itself has no dependencies.

Delegation is just a reservation against the parent. For every budget:

```
consumed + active reservations + delegated child shares <= limit
```

| Call | Hook it maps to | What it does |
|---|---|---|
| `reserve(budgetRef, amount)` | before the action / before signing | Holds the amount, or refuses with the remaining balance |
| `settle(reservation, actual)` | after success | Records what was actually spent (capped at the hold) |
| `release(reservation)` | on failure | Frees the hold without spending |
| `delegate(parentRef, childId, max)` | spawning a sub-agent | Carves a share out of the parent's remaining budget |
| `revoke(ref)` | stopping an agent | Stops the subtree; unspent shares return to the parent once nothing is in flight |

Each balance-changing call is synchronous, so within one JS process two
concurrent callers can't both take the last unit of a budget, and siblings
can't both delegate the parent's last unit.

## Audit log

Pass a second argument to keep an append-only log of every balance change and
refusal, each with its parent ref:

```ts
const provider = new HierarchicalBudgetProvider(Date.now, (e) => log.write(JSON.stringify(e, (_k, v) => (typeof v === "bigint" ? v.toString() : v)) + "\n"));
// {"op":"delegate","ref":"job-42/research","parent":"job-42","amount":"1000000","seq":2,"at":...}
```

Ops: `open`, `delegate`, `reserve`, `settle`, `release` (with `expired`),
`refuse`, `revoke`, `return` (a revoked child's unspent share going back to its
parent). Sequence numbers are gapless, so a third party can rebuild every
budget from the log alone and check the invariant without trusting the
provider; the tests do exactly that.

## Put an x402 client under job budgets

`x402-job-budget.ts` connects the provider to any `x402Client` from
`@x402/core`, and so to `@x402/fetch` and `@x402/axios`. Every payment the
client signs is drawn from the budget that's active for the calling task, and
is refused **before signing** when that budget can't cover it. With no budget
active, nothing is signed.

```ts
import { x402Client } from "@x402/core/client";
import { registerExactEvmScheme } from "@x402/evm/exact/client";
import { wrapFetchWithPayment } from "@x402/fetch";
import { HierarchicalBudgetProvider } from "./hierarchical-budget-provider.ts";
import { withJobBudgets } from "./x402-job-budget.ts";

const client = new x402Client();
registerExactEvmScheme(client, { signer });            // your existing signer
const provider = new HierarchicalBudgetProvider();
const budgets = withJobBudgets(client, provider);
const paidFetch = wrapFetchWithPayment(fetch, client);

provider.open("job-42", 5_000_000n);                    // $5.00 USDC for the job
const research = provider.delegate("job-42", "research", 1_000_000n);
const browser = provider.delegate("job-42", "browser", 500_000n);
if (!research.ok || !browser.ok) throw new Error("not enough budget");

// Concurrent sub-agents, each bounded by its own share
await Promise.all([
  budgets.run(research.childRef, () => researchAgent(paidFetch)),
  budgets.run(browser.childRef, () => browserAgent(paidFetch)),
]);

provider.status("job-42");                              // consumed, reserved, delegated, remaining
provider.revoke(browser.childRef);                      // stop one child; its unspent share returns
```

Each payment gets its own reservation, held from the before-hook until the
call returns a signed payload (counted as spent) or fails (released). This is
done around the whole `createPaymentPayload()` call, not in the after and
failure hooks, because `@x402/core` gives each hook phase a new context object
and skips the failure hooks when a later before-hook aborts
([x402-foundation/x402#3703](https://github.com/x402-foundation/x402/issues/3703)).

## ElizaOS agents (plugin-wallet)

`eliza-x402-budgets.ts` does the same for ElizaOS agents that pay with
plugin-wallet's x402 client. It uses the client's payment callbacks
(`onBeforePayment`, `onPaymentComplete` and `onPaymentFailed`, with one
`paymentId` per attempt). Those landed on Eliza's `develop` in
elizaOS/eliza#34131 and aren't in the npm beta yet.

```ts
import { createX402Client } from "@elizaos/plugin-wallet/sdk/index";   // develop
import { HierarchicalBudgetProvider } from "./hierarchical-budget-provider.ts";
import { elizaX402Budgets } from "./eliza-x402-budgets.ts";

const provider = new HierarchicalBudgetProvider();
const budgets = elizaX402Budgets(provider);
const client = createX402Client(wallet, { ...budgets.callbacks });

provider.open("agent:eliza", 5_000_000n);                     // $5.00 USDC for this agent
const task = provider.delegate("agent:eliza", "task-17", 1_000_000n);
if (task.ok) await budgets.run(task.childRef, () => client.fetch(url));
```

A payment that doesn't fit the active budget is declined in `onBeforePayment`,
so plugin-wallet returns the original 402 and nothing is transferred
(`budgets.lastRefusal(ref)` says why). A failed payment whose transfer was
already submitted is counted as spent, since it may still land.

## Run the tests

```bash
cd examples/agent_economy/typescript
bun install    # @x402/core, @x402/evm and viem, for the adapter tests only
bun test       # 17 tests; the adapter tests sign real EIP-3009 payloads locally
```

Scope, plainly: it's in-memory and single-process. A signed payload is
counted when it's created, even if the seller never settles it. For the payer
key to stay out of your agents, run the client in the process that holds the
signer and hand agents budget refs, not the client. For durability across
restarts and processes, run the same operations inside a database transaction,
as the Python ledger in `hosted/a2a_economic_authority/core.py` does with
SQLite (used by the runnable example one directory up).
