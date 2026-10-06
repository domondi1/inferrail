/**
 * Put an x402 client under job budgets: every payment it signs is drawn from
 * the budget (or child-agent share) that is active for the calling task, and is
 * refused before signing when that budget can't cover it.
 *
 * Works with any `x402Client` from `@x402/core`, so also with `@x402/fetch`
 * (`wrapFetchWithPayment`) and `@x402/axios`, which call it to pay.
 *
 *     const budgets = withJobBudgets(client, provider);
 *     provider.open("job-42", 5_000_000n);                       // $5.00 USDC
 *     const r = provider.delegate("job-42", "researcher", 1_000_000n);
 *     await budgets.run(r.childRef, () => paidFetch(url));      // draws from $1.00
 *
 * Each `createPaymentPayload()` call gets its own reservation, held from the
 * before-hook until the call returns a signed payload (counted as spent) or
 * throws (released). That's done around the whole call rather than in the
 * after/failure hooks, because `@x402/core` gives each hook phase a new context
 * object and skips the failure hooks when a later before-hook aborts
 * (x402-foundation/x402#3703).
 *
 * Scope, plainly: one process, in memory (like the provider). A signed payload
 * is counted when it is created, even if the seller then never settles it. For
 * the payer key to stay out of the agents, run this in the process that holds
 * the signer and give agents budget refs, not the client.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import type { x402Client } from "@x402/core/client";
import type { BudgetRef, BudgetReservation, HierarchicalBudgetProvider } from "./hierarchical-budget-provider.ts";

interface CallSlot {
  budgetRef: BudgetRef | undefined;
  reservation?: BudgetReservation;
}

export interface JobBudgets {
  /** Run `fn` with every x402 payment it makes drawn from `budgetRef`. */
  run<T>(budgetRef: BudgetRef, fn: () => Promise<T>): Promise<T>;
}

export function withJobBudgets(client: x402Client, provider: HierarchicalBudgetProvider): JobBudgets {
  const activeBudget = new AsyncLocalStorage<BudgetRef>();
  const currentCall = new AsyncLocalStorage<CallSlot>();

  client.onBeforePaymentCreation(async ({ selectedRequirements }) => {
    const slot = currentCall.getStore();
    const budgetRef = slot?.budgetRef;
    if (!slot || budgetRef === undefined) {
      return { abort: true, reason: "no job budget is active for this payment" };
    }
    const result = provider.reserve(budgetRef, BigInt(selectedRequirements.amount));
    if (!result.ok) {
      return {
        abort: true,
        reason: `${result.reason}: ${budgetRef} has ${result.remainingAtomic} left, payment needs ${selectedRequirements.amount}`,
      };
    }
    slot.reservation = result.reservation;
  });

  const createPaymentPayload = client.createPaymentPayload.bind(client);
  client.createPaymentPayload = async (paymentRequired) => {
    const slot: CallSlot = { budgetRef: activeBudget.getStore() };
    try {
      const payload = await currentCall.run(slot, () => createPaymentPayload(paymentRequired));
      if (slot.reservation) provider.settle(slot.reservation, slot.reservation.amountAtomic);
      return payload;
    } catch (error) {
      if (slot.reservation) provider.release(slot.reservation);
      throw error;
    }
  };

  return {
    run: (budgetRef, fn) => activeBudget.run(budgetRef, fn),
  };
}
