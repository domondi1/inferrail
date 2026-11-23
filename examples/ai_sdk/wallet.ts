/**
 * A paying fetch for the examples, under limits the user sets. Nothing here creates or funds a
 * wallet: it uses the key the user put in EVM_PRIVATE_KEY (a wallet holding a little USDC on
 * Base), and stops paying once this run has spent MAX_USD.
 */

import { guardInferrailPrices } from "@inferrail/ai-sdk-tools";
import { registerExactEvmScheme } from "@x402/evm/exact/client";
import { wrapFetchWithPayment, x402Client } from "@x402/fetch";
import { privateKeyToAccount } from "viem/accounts";

/** The before-payment hook shape shared by @x402/core clients. */
export interface Hookable {
  onBeforePaymentCreation(
    hook: (ctx: {
      paymentRequired: { resource?: { url?: string } };
      selectedRequirements: { amount: string; payTo: string };
    }) => Promise<void | { abort: true; reason: string }>,
  ): unknown;
}

/** USDC base units (6 decimals) for a dollar amount like "0.01". */
export function toAtomic(usd: string): bigint {
  const [whole, frac = ""] = usd.trim().split(".");
  if (!/^\d+$/.test(whole) || !/^\d*$/.test(frac) || frac.length > 6) {
    throw new Error(`not a USD amount: ${usd}`);
  }
  return BigInt(whole) * 1_000_000n + BigInt(frac.padEnd(6, "0"));
}

/** Refuse to sign any payment that would take this run past `maxUsd` in total. A refused
 * payment is never signed, so nothing is charged for it. Returns what's been reserved so far. */
export function capSpending(client: Hookable, maxUsd: string): () => bigint {
  const cap = toAtomic(maxUsd);
  let spent = 0n;
  client.onBeforePaymentCreation(async ({ selectedRequirements }) => {
    const amount = BigInt(selectedRequirements.amount);
    if (spent + amount > cap) {
      return { abort: true, reason: `spending cap: ${maxUsd} USD for this run` };
    }
    spent += amount; // reserved before signing; a payment that then isn't charged still counts
  });
  return () => spent;
}

/** The paying fetch for a run: the user's own key, Inferrail's price guard, and the cap. */
export function paidFetchFromEnv(env: Record<string, string | undefined> = process.env) {
  const key = env.EVM_PRIVATE_KEY;
  if (!key || !/^0x[0-9a-fA-F]{64}$/.test(key)) {
    throw new Error(
      "Set EVM_PRIVATE_KEY to a wallet you control that holds a little USDC on Base. " +
        "These examples never create or fund a wallet.",
    );
  }
  const maxUsd = env.MAX_USD ?? "0.01";
  const client = new x402Client();
  registerExactEvmScheme(client, { signer: privateKeyToAccount(key as `0x${string}`) });
  guardInferrailPrices(client);
  const spent = capSpending(client, maxUsd);
  return { fetch: wrapFetchWithPayment(fetch, client), spent, maxUsd };
}
