# Hierarchical budget provider (TypeScript reference)

A small, dependency-free reference for spend-policy hooks that need **sub-agent
budgets**: for example, a pre-execution `policyProvider` in front of x402 or
wallet actions.

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

```bash
bun test examples/agent_economy/typescript
```

Scope, plainly: it's in-memory and single-process. For durability across
restarts and processes, run the same operations inside a database transaction,
as the Python ledger in `hosted/a2a_economic_authority/core.py` does with
SQLite (used by the runnable example one directory up).
