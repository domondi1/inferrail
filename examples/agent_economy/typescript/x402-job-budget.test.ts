import { describe, expect, test } from "bun:test";
import { x402Client } from "@x402/core/client";
import { toClientEvmSigner } from "@x402/evm";
import { registerExactEvmScheme } from "@x402/evm/exact/client";
import { privateKeyToAccount } from "viem/accounts";
import { HierarchicalBudgetProvider } from "./hierarchical-budget-provider.ts";
import { withJobBudgets } from "./x402-job-budget.ts";

// Real @x402/core client and EIP-3009 signing with a throwaway key; no network.
const signer = toClientEvmSigner(privateKeyToAccount(`0x${"11".repeat(32)}`));

/** A 402 asking for `atomic` USDC (6 decimals) on Base Sepolia. */
function paymentRequired(atomic: bigint) {
  return {
    x402Version: 2,
    resource: { url: "https://paid.example/search" },
    accepts: [
      {
        scheme: "exact",
        network: "eip155:84532",
        amount: atomic.toString(),
        asset: "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        payTo: "0x4f27DC247a55EA5920F8311A25672Ed1B590792d",
        maxTimeoutSeconds: 60,
        extra: { name: "USDC", version: "2" },
      },
    ],
  } as unknown as Parameters<x402Client["createPaymentPayload"]>[0];
}

function setup() {
  const client = new x402Client();
  registerExactEvmScheme(client, { signer });
  const provider = new HierarchicalBudgetProvider();
  const budgets = withJobBudgets(client, provider);
  return { client, provider, budgets };
}

const pay = (client: x402Client, atomic: bigint) => client.createPaymentPayload(paymentRequired(atomic));

describe("withJobBudgets", () => {
  test("a payment is signed and counted against the active job budget", async () => {
    const { client, provider, budgets } = setup();
    provider.open("job", 50_000n);

    const payload = await budgets.run("job", () => pay(client, 20_000n));

    expect(payload.payload).toBeDefined();
    expect(provider.status("job")?.consumed).toBe(20_000n);
    expect(provider.remaining("job")).toBe(30_000n);
  });

  test("a payment over the remaining budget is refused before signing", async () => {
    const { client, provider, budgets } = setup();
    provider.open("job", 10_000n);

    await expect(budgets.run("job", () => pay(client, 20_000n))).rejects.toThrow(/insufficient_budget/);
    expect(provider.status("job")?.consumed).toBe(0n);
    expect(provider.status("job")?.reserved).toBe(0n);
  });

  test("with no budget active, nothing is signed (fails closed)", async () => {
    const { client } = setup();
    await expect(pay(client, 1n)).rejects.toThrow(/no job budget is active/);
  });

  test("concurrent payments cannot spend the same remaining budget", async () => {
    const { client, provider, budgets } = setup();
    provider.open("job", 50_000n);

    const results = await Promise.allSettled(
      Array.from({ length: 20 }, () => budgets.run("job", () => pay(client, 10_000n))),
    );

    expect(results.filter((r) => r.status === "fulfilled")).toHaveLength(5);
    expect(provider.status("job")?.consumed).toBe(50_000n);
    expect(provider.status("job")?.reserved).toBe(0n);
  });

  test("concurrent child agents each stay inside their own share", async () => {
    const { client, provider, budgets } = setup();
    provider.open("job", 100_000n);
    const research = provider.delegate("job", "research", 30_000n);
    const browser = provider.delegate("job", "browser", 20_000n);
    if (!research.ok || !browser.ok) throw new Error("delegation failed");

    const loop = (ref: string) =>
      Promise.allSettled(Array.from({ length: 10 }, () => budgets.run(ref, () => pay(client, 10_000n))));
    const [r, b] = await Promise.all([loop(research.childRef), loop(browser.childRef)]);

    expect(r.filter((x) => x.status === "fulfilled")).toHaveLength(3);
    expect(b.filter((x) => x.status === "fulfilled")).toHaveLength(2);
    expect(provider.status(research.childRef)?.consumed).toBe(30_000n);
    expect(provider.status(browser.childRef)?.consumed).toBe(20_000n);
    // The parent still has its undelegated $0.05
    expect(provider.remaining("job")).toBe(50_000n);
  });

  test("a later before-hook that aborts releases the hold", async () => {
    const { client, provider, budgets } = setup();
    provider.open("job", 10_000n);
    let block = true;
    client.onBeforePaymentCreation(async () => (block ? { abort: true, reason: "policy" } : undefined));

    await expect(budgets.run("job", () => pay(client, 10_000n))).rejects.toThrow(/policy/);
    expect(provider.status("job")?.reserved).toBe(0n);

    block = false;
    await budgets.run("job", () => pay(client, 10_000n));
    expect(provider.status("job")?.consumed).toBe(10_000n);
  });

  test("a failing call never settles another call's reservation", async () => {
    const { client, provider, budgets } = setup();
    provider.open("job", 20_000n);
    let releaseSecond!: () => void;
    const gate = new Promise<void>((r) => {
      releaseSecond = r;
    });
    let befores = 0;
    client.onBeforePaymentCreation(async () => {
      if (++befores === 2) await gate;
    });
    let afters = 0;
    client.onAfterPaymentCreation(async () => {
      if (++afters === 1) throw new Error("after-hook failed");
    });

    const first = budgets.run("job", () => pay(client, 10_000n));
    const second = budgets.run("job", () => pay(client, 10_000n));
    await expect(first).rejects.toThrow("after-hook failed");
    expect(provider.status("job")?.reserved).toBe(10_000n); // second still held
    expect(provider.status("job")?.consumed).toBe(0n);

    releaseSecond();
    await second;
    expect(provider.status("job")?.consumed).toBe(10_000n);
    expect(provider.status("job")?.reserved).toBe(0n);
  });

  test("a revoked child can't start new payments", async () => {
    const { client, provider, budgets } = setup();
    provider.open("job", 50_000n);
    const child = provider.delegate("job", "worker", 30_000n);
    if (!child.ok) throw new Error("delegation failed");
    provider.revoke(child.childRef);

    await expect(budgets.run(child.childRef, () => pay(client, 1_000n))).rejects.toThrow(/revoked/);
    expect(provider.remaining("job")).toBe(50_000n);
  });
});
