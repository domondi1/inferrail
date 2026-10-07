# Recipe: budgets for agents that pay

**Check-then-pay is not a budget.** If your agents pay for things (x402 APIs,
USDC transfers, other agents), this is the bug most spend limits have, how to
fix it, and what a budget for a job and its sub-agents needs on top.

Experimental. The code here runs locally against a simulated chain; there is
no mainnet support.

## The bug

Almost every agent spend cap is written like this:

```ts
if (spent + amount > limit) throw new Error("over budget");  // check
await pay(amount);                                           // sign + transfer: takes a while
spent += amount;                                             // record
```

An agent that makes parallel tool calls, or a swarm with several workers on
one wallet, runs that function several times at once. Every call reads the
same `spent` before any of them records, so ten concurrent $1 payments against
a $5 cap all go through.

This isn't hypothetical. The same shape was reported, reproduced and fixed in
three agent products in one week:

- [desplega-ai/agent-swarm#1885](https://github.com/desplega-ai/agent-swarm/pull/1885): the x402 client's daily limit, with workers paying concurrently.
- [elizaOS/eliza#33937](https://github.com/elizaOS/eliza/pull/33937): plugin-wallet's per-service daily budget.
- [Bitterbot-AI/bitterbot-desktop#157](https://github.com/Bitterbot-AI/bitterbot-desktop/pull/157): the wallet's daily cap. Under contention, 6 of 10 callers were also told "Failed to send USDC" for transfers that had already gone out, so an agent retrying on that error pays twice.

## The fix: reserve, then settle or release

Hold the amount in the same step as the check, with nothing awaited in
between. Then turn the hold into spend, or give it back:

```ts
const hold = reserve(amount);   // checks spent + held + amount <= limit, and holds it, synchronously
try {
  await pay(amount);
  settle(hold);                 // the hold becomes spend
} catch (error) {
  release(hold);                // only if nothing could have been sent
  throw error;
}
```

Two details matter in practice:

- **Settle what was actually charged**, which can be less than the hold.
- **An ambiguous failure isn't a failure.** After a timeout, the transfer may
  have landed. Don't release the hold. Keep it held until you know the
  outcome. For EIP-3009 (`exact` on EVM), the token contract tells you:
  `authorizationState(payer, nonce)` says whether the authorization was used,
  and after `validBefore` it can never settle.

## A flat cap still isn't a job budget

Once the race is fixed, the next problem is scope. A wallet or session cap is
shared by everything that runs under it: one task looping on a paid call
uses up the day for every other task, and you can't stop one sub-agent
without stopping all of them.

What you want instead:

- **one budget per job**;
- **bounded shares for sub-agents**, carved out of the parent's remaining
  budget when they're created, so siblings can't both be given the parent's
  last dollar;
- **refusal before signing** when a payment doesn't fit the caller's share;
- **revoking one sub-agent** stops its new payments, and its unspent share goes
  back to the parent;
- **the payer key in one process**. Agents get budget refs, not the key.

For every budget the rule is
`consumed + active holds + delegated child shares <= limit`, enforced in one
step.

## Do it with an existing x402 client (TypeScript)

```ts
import { HierarchicalBudgetProvider } from "./hierarchical-budget-provider.ts";
import { withJobBudgets } from "./x402-job-budget.ts";

const provider = new HierarchicalBudgetProvider();
const budgets = withJobBudgets(client, provider);       // your @x402/core client
const paidFetch = wrapFetchWithPayment(fetch, client);   // @x402/fetch

provider.open("job-42", 5_000_000n);                     // $5.00 USDC
const research = provider.delegate("job-42", "research", 1_000_000n);
if (research.ok) await budgets.run(research.childRef, () => researchAgent(paidFetch));
```

Code and tests:
[examples/agent_economy/typescript](../../examples/agent_economy/typescript/)
(also covers ElizaOS plugin-wallet).

## See the whole thing run

```bash
git clone https://github.com/domondi1/inferrail && cd inferrail
pip install "x402[evm,fastapi,httpx]==2.22.0" uvicorn
python examples/agent_economy/demo.py
```

One job budget covers model calls and x402 purchases. A sub-agent gets a
share, concurrent purchases can't pass it, and the over-budget ones are
refused before anything is signed. Real x402 and EIP-3009 signatures; a
simulated USDC chain. Details:
[examples/agent_economy](../../examples/agent_economy/).

Questions, or an agent you'd like to wire this into:
[open an issue](https://github.com/domondi1/inferrail/issues).
