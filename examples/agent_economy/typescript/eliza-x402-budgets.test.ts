import { describe, expect, test } from "bun:test";
import { elizaX402Budgets, type ElizaPaymentCallbacks } from "./eliza-x402-budgets.ts";
import { HierarchicalBudgetProvider } from "./hierarchical-budget-provider.ts";

/**
 * Drives the callbacks in plugin-wallet X402Client's order: onBeforePayment,
 * then (after an async transfer) onPaymentComplete or onPaymentFailed, with one
 * paymentId per attempt. Returns "paid", "declined" or "failed".
 */
async function pay(cb: ElizaPaymentCallbacks, amount: bigint, outcome: "ok" | "fail-before" | "fail-after" = "ok") {
  const attempt = { paymentId: crypto.randomUUID() };
  if (!cb.onBeforePayment({ amount: amount.toString() }, "https://paid.example/r", attempt)) return "declined";
  await new Promise((r) => setTimeout(r, 5)); // the transfer
  if (outcome === "ok") {
    cb.onPaymentComplete({ amount }, attempt);
    return "paid";
  }
  cb.onPaymentFailed({}, "https://paid.example/r", new Error("x"), {
    ...attempt,
    transferAttempted: outcome === "fail-after",
  });
  return "failed";
}

function setup() {
  const provider = new HierarchicalBudgetProvider();
  const budgets = elizaX402Budgets(provider);
  return { provider, budgets };
}

describe("elizaX402Budgets", () => {
  test("a payment is held, then counted when it completes", async () => {
    const { provider, budgets } = setup();
    provider.open("agent", 50_000n);
    expect(await budgets.run("agent", () => pay(budgets.callbacks, 20_000n))).toBe("paid");
    expect(provider.status("agent")).toMatchObject({ consumed: 20_000n, reserved: 0n });
  });

  test("with no budget active, the payment is declined", async () => {
    const { budgets } = setup();
    expect(await pay(budgets.callbacks, 1n)).toBe("declined");
  });

  test("concurrent payments under one task cannot pass its share", async () => {
    const { provider, budgets } = setup();
    provider.open("agent", 100_000n);
    const t = provider.delegate("agent", "task-17", 30_000n);
    if (!t.ok) throw new Error("delegation failed");

    const results = await budgets.run(t.childRef, () =>
      Promise.all(Array.from({ length: 10 }, () => pay(budgets.callbacks, 10_000n))),
    );

    expect(results.filter((r) => r === "paid")).toHaveLength(3);
    expect(provider.status(t.childRef)).toMatchObject({ consumed: 30_000n, reserved: 0n });
    expect(budgets.lastRefusal(t.childRef)).toMatch(/insufficient_budget/);
    expect(provider.remaining("agent")).toBe(70_000n); // the rest of the agent's budget is untouched
  });

  test("a failure before any transfer frees the hold; after a transfer it is counted", async () => {
    const { provider, budgets } = setup();
    provider.open("agent", 10_000n);
    expect(await budgets.run("agent", () => pay(budgets.callbacks, 10_000n, "fail-before"))).toBe("failed");
    expect(provider.remaining("agent")).toBe(10_000n);

    expect(await budgets.run("agent", () => pay(budgets.callbacks, 10_000n, "fail-after"))).toBe("failed");
    expect(provider.status("agent")).toMatchObject({ consumed: 10_000n, reserved: 0n });
  });

  test("revoking one task stops its payments and leaves the others alone", async () => {
    const { provider, budgets } = setup();
    provider.open("agent", 100_000n);
    const a = provider.delegate("agent", "a", 20_000n);
    const b = provider.delegate("agent", "b", 20_000n);
    if (!a.ok || !b.ok) throw new Error("delegation failed");
    provider.revoke(a.childRef);

    expect(await budgets.run(a.childRef, () => pay(budgets.callbacks, 1_000n))).toBe("declined");
    expect(await budgets.run(b.childRef, () => pay(budgets.callbacks, 1_000n))).toBe("paid");
  });
});
