# @inferrail/x402-budgets

Give an AI agent purchasing power without giving it the whole wallet.

- **One budget per job.** Every payment the job's agents make draws from it.
- **Bounded shares for sub-agents**, carved out of the parent's budget. A
  sub-agent can't spend a sibling's share, even when they run at the same time.
- **Refused before signing.** A payment that doesn't fit is never signed, and
  the error tells you what's left.
- **Stop one branch.** Revoking a sub-agent stops its new payments; its unspent
  share goes back to the parent.
- **Audit log** (optional): every balance change and refusal, with parent refs,
  so the whole tree can be rebuilt and checked from the log alone.

Experimental (0.x), in memory, one process. Keep the payer key in the process
that runs this, and give sub-agents budget refs, not the client.

```bash
npm i @inferrail/x402-budgets
```

## With an x402 client (@x402/core, @x402/fetch, @x402/axios)

```ts
import { HierarchicalBudgetProvider, withJobBudgets } from "@inferrail/x402-budgets";

const provider = new HierarchicalBudgetProvider();
const budgets = withJobBudgets(client, provider);       // your existing x402Client
const paidFetch = wrapFetchWithPayment(fetch, client);

provider.open("job-42", 5_000_000n);                     // $5.00 USDC (6 decimals)
const research = provider.delegate("job-42", "research", 1_000_000n);
const browser = provider.delegate("job-42", "browser", 500_000n);
if (!research.ok || !browser.ok) throw new Error("not enough budget");

await Promise.all([
  budgets.run(research.childRef, () => researchAgent(paidFetch)),
  budgets.run(browser.childRef, () => browserAgent(paidFetch)),
]);

provider.status("job-42");   // { limit, consumed, reserved, delegated, remaining, revoked }
```

With no budget active, nothing is signed. Each `createPaymentPayload()` call
holds its own reservation from the before-hook until it returns a signed
payload (counted) or throws (released).

## With ElizaOS plugin-wallet

Uses the x402 client's `paymentId` / `onPaymentFailed` callbacks (Eliza
`develop`, elizaOS/eliza#34131):

```ts
import { HierarchicalBudgetProvider, elizaX402Budgets } from "@inferrail/x402-budgets";

const provider = new HierarchicalBudgetProvider();
const budgets = elizaX402Budgets(provider);
const client = createX402Client(wallet, { ...budgets.callbacks });

provider.open("agent:eliza", 5_000_000n);
const task = provider.delegate("agent:eliza", "task-17", 1_000_000n);
if (task.ok) await budgets.run(task.childRef, () => client.fetch(url));
```

## The budget rules on their own

```ts
const p = new HierarchicalBudgetProvider(Date.now, (event) => log(event));
p.open("job", 10_000n);
const child = p.delegate("job", "worker", 4_000n);
const hold = child.ok && p.reserve(child.childRef, 1_000n);
if (hold && hold.ok) p.settle(hold.reservation, 900n);   // or p.release(hold.reservation)
```

For every budget: `consumed + active reservations + delegated child shares <= limit`,
enforced in one synchronous step per call.

## More

Source, tests and a runnable end-to-end proof (Python runtime, real x402 and
EIP-3009 signatures, simulated chain):
https://github.com/domondi1/inferrail/tree/main/examples/agent_economy

Questions or an agent you'd like to wire it into:
https://github.com/domondi1/inferrail/issues

Apache-2.0.
