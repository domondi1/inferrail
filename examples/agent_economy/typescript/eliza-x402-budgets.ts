/**
 * Job budgets for ElizaOS agents that pay with plugin-wallet's x402 client.
 *
 * plugin-wallet's `X402Client` calls `onBeforePayment`, then either
 * `onPaymentComplete` or `onPaymentFailed`, with one `paymentId` per attempt
 * (elizaOS/eliza#34131, on `develop`). This turns those callbacks into a
 * hierarchical budget: hold the amount before paying, settle it when the
 * payment completes, release it when the payment fails before any transfer.
 *
 *     const budgets = elizaX402Budgets(provider);
 *     const client = createX402Client(wallet, { ...budgets.callbacks });
 *     provider.open("agent:eliza", 5_000_000n);                  // $5.00 USDC
 *     const t = provider.delegate("agent:eliza", "task-17", 1_000_000n);
 *     await budgets.run(t.childRef, () => client.fetch(url));    // draws from $1.00
 *
 * A payment with no budget active, or one that doesn't fit, is declined in
 * `onBeforePayment`, so plugin-wallet returns the original 402 and nothing is
 * transferred. A failed payment whose transfer was already submitted is counted
 * as spent, because it may still land: the budget errs toward underspending.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import type { BudgetRef, BudgetReservation, HierarchicalBudgetProvider } from "./hierarchical-budget-provider.ts";

/** The parts of plugin-wallet's payment callbacks this needs. */
export interface ElizaPaymentCallbacks {
  onBeforePayment: (req: { amount: string }, url: string, attempt: { paymentId: string }) => boolean;
  onPaymentComplete: (log: { amount: bigint }, attempt: { paymentId: string }) => void;
  onPaymentFailed: (
    req: unknown,
    url: string,
    error: unknown,
    attempt: { paymentId: string; transferAttempted: boolean },
  ) => void;
}

export interface ElizaX402Budgets {
  /** Pass these to `createX402Client(wallet, { ...callbacks })`. */
  callbacks: ElizaPaymentCallbacks;
  /** Run `fn` with every x402 payment it makes drawn from `budgetRef`. */
  run<T>(budgetRef: BudgetRef, fn: () => Promise<T>): Promise<T>;
  /** Why the last payment for this budget was declined, if it was. */
  lastRefusal(budgetRef: BudgetRef): string | undefined;
}

export function elizaX402Budgets(provider: HierarchicalBudgetProvider): ElizaX402Budgets {
  const active = new AsyncLocalStorage<BudgetRef>();
  const holds = new Map<string, BudgetReservation>();
  const refusals = new Map<BudgetRef, string>();

  return {
    callbacks: {
      onBeforePayment: (req, _url, { paymentId }) => {
        const budgetRef = active.getStore();
        if (budgetRef === undefined) return false;
        const result = provider.reserve(budgetRef, BigInt(req.amount));
        if (!result.ok) {
          refusals.set(budgetRef, `${result.reason}: ${result.remainingAtomic} left, needs ${req.amount}`);
          return false;
        }
        holds.set(paymentId, result.reservation);
        return true;
      },
      onPaymentComplete: (log, { paymentId }) => {
        const hold = holds.get(paymentId);
        if (!hold) return;
        holds.delete(paymentId);
        provider.settle(hold, log.amount);
      },
      onPaymentFailed: (_req, _url, _error, { paymentId, transferAttempted }) => {
        const hold = holds.get(paymentId);
        if (!hold) return;
        holds.delete(paymentId);
        // A submitted transfer may still land, so count it rather than free it
        if (transferAttempted) provider.settle(hold, hold.amountAtomic);
        else provider.release(hold);
      },
    },
    run: (budgetRef, fn) => active.run(budgetRef, fn),
    lastRefusal: (budgetRef) => refusals.get(budgetRef),
  };
}
