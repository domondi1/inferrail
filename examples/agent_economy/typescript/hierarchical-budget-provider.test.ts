import { describe, expect, test } from "bun:test";
import { type BudgetEvent, HierarchicalBudgetProvider } from "./hierarchical-budget-provider.ts";

/** consumed + reserved + delegated <= limit, and nothing negative, for every budget. */
function expectInvariant(p: HierarchicalBudgetProvider, refs: string[]) {
  for (const ref of refs) {
    const s = p.status(ref);
    if (!s) continue;
    expect(s.consumed >= 0n && s.reserved >= 0n && s.delegated >= 0n).toBe(true);
    expect(s.consumed + s.reserved + s.delegated <= s.limit).toBe(true);
  }
}

describe("HierarchicalBudgetProvider", () => {
  test("reserve then settle records the actual amount (capped at the reservation)", () => {
    const p = new HierarchicalBudgetProvider();
    p.open("job", 10_000n);
    const r = p.reserve("job", 3_000n);
    expect(r.ok).toBe(true);
    if (r.ok) p.settle(r.reservation, 2_000n);
    expect(p.status("job")?.consumed).toBe(2_000n);
    expect(p.remaining("job")).toBe(8_000n);
  });

  test("concurrent reservations cannot exceed the budget", async () => {
    const p = new HierarchicalBudgetProvider();
    p.open("job", 10_000n);
    const results = await Promise.all(
      Array.from({ length: 20 }, async () => p.reserve("job", 2_000n)),
    );
    expect(results.filter((r) => r.ok).length).toBe(5);
    expect(p.remaining("job")).toBe(0n);
    expectInvariant(p, ["job"]);
  });

  test("siblings cannot both delegate the parent's last units", () => {
    const p = new HierarchicalBudgetProvider();
    p.open("job", 10_000n);
    const a = p.delegate("job", "researcher", 6_000n);
    const b = p.delegate("job", "browser", 6_000n);
    expect(a.ok).toBe(true);
    expect(b.ok).toBe(false);
    if (!b.ok) expect(b.remainingAtomic).toBe(4_000n);
    expectInvariant(p, ["job", "job/researcher"]);
  });

  test("a child can only spend its own share, even if the parent has more", () => {
    const p = new HierarchicalBudgetProvider();
    p.open("job", 10_000n);
    const d = p.delegate("job", "researcher", 2_000n);
    expect(d.ok).toBe(true);
    const child = d.ok ? d.childRef : "";
    expect(p.reserve(child, 1_500n).ok).toBe(true);
    const over = p.reserve(child, 1_000n);
    expect(over.ok).toBe(false);
    if (!over.ok) {
      expect(over.reason).toBe("insufficient_budget");
      expect(over.remainingAtomic).toBe(500n);
    }
    expect(p.remaining("job")).toBe(8_000n);
  });

  test("release frees a reservation without spending", () => {
    const p = new HierarchicalBudgetProvider();
    p.open("job", 5_000n);
    const r = p.reserve("job", 5_000n);
    expect(p.reserve("job", 1n).ok).toBe(false);
    if (r.ok) p.release(r.reservation);
    expect(p.status("job")?.consumed).toBe(0n);
    expect(p.reserve("job", 1n).ok).toBe(true);
  });

  test("revoking an idle child returns its unspent share; spent units stay counted", () => {
    const p = new HierarchicalBudgetProvider();
    p.open("job", 10_000n);
    const d = p.delegate("job", "researcher", 4_000n);
    const child = d.ok ? d.childRef : "";
    const r = p.reserve(child, 1_000n);
    if (r.ok) p.settle(r.reservation, 1_000n);
    expect(p.remaining("job")).toBe(6_000n);
    p.revoke(child);
    expect(p.remaining("job")).toBe(9_000n); // 3_000 unspent came back, 1_000 spent stays
    expect(p.reserve(child, 1n).ok).toBe(false);
    expectInvariant(p, ["job", child]);
  });

  test("revoking with a reservation in flight keeps the hold until it settles", () => {
    const p = new HierarchicalBudgetProvider();
    p.open("job", 10_000n);
    const d = p.delegate("job", "browser", 3_000n);
    const child = d.ok ? d.childRef : "";
    const inFlight = p.reserve(child, 2_000n);
    p.revoke(child);
    const refused = p.reserve(child, 1n);
    expect(refused.ok).toBe(false);
    if (!refused.ok) expect(refused.reason).toBe("revoked");
    expect(p.remaining("job")).toBe(7_000n); // share still held while in flight
    if (inFlight.ok) p.settle(inFlight.reservation, 500n);
    expect(p.remaining("job")).toBe(9_500n); // unspent 2_500 returned once idle
    expectInvariant(p, ["job", child]);
  });

  test("revoking a parent stops the whole subtree", () => {
    const p = new HierarchicalBudgetProvider();
    p.open("job", 10_000n);
    const d = p.delegate("job", "researcher", 4_000n);
    const child = d.ok ? d.childRef : "";
    const g = p.delegate(child, "helper", 1_000n);
    const grandchild = g.ok ? g.childRef : "";
    p.revoke("job");
    expect(p.reserve(grandchild, 1n).ok).toBe(false);
    expect(p.delegate(child, "another", 1n).ok).toBe(false);
  });

  test("an expired reservation is released automatically", () => {
    let t = 1_000;
    const p = new HierarchicalBudgetProvider(() => t);
    p.open("job", 5_000n);
    const r = p.reserve("job", 5_000n, 100);
    expect(r.ok).toBe(true);
    t += 101;
    expect(p.reserve("job", 5_000n).ok).toBe(true); // stale hold was released
    if (r.ok) p.settle(r.reservation, 5_000n); // late settle of an expired hold is a no-op
    expect(p.status("job")?.consumed).toBe(0n);
  });
});

describe("event log", () => {
  /** Rebuild every budget's balances from the log alone, as an outside verifier would. */
  function replay(events: BudgetEvent[]) {
    const b = new Map<string, { parent?: string; limit: bigint; consumed: bigint; reserved: bigint; delegated: bigint }>();
    const holds = new Map<string, { parent: string; amount: bigint }>();
    for (const e of events) {
      if (e.op === "open") b.set(e.ref, { limit: e.amount, consumed: 0n, reserved: 0n, delegated: 0n });
      if (e.op === "delegate") {
        b.get(e.parent)!.delegated += e.amount;
        b.set(e.ref, { parent: e.parent, limit: e.amount, consumed: 0n, reserved: 0n, delegated: 0n });
      }
      if (e.op === "reserve") {
        b.get(e.parent)!.reserved += e.amount;
        holds.set(e.ref, { parent: e.parent, amount: e.amount });
      }
      if (e.op === "settle" || e.op === "release") {
        const h = holds.get(e.ref)!;
        holds.delete(e.ref);
        b.get(h.parent)!.reserved -= h.amount;
        if (e.op === "settle") b.get(h.parent)!.consumed += e.amount;
      }
      if (e.op === "return") {
        b.get(e.parent)!.delegated -= e.amount;
        b.get(e.ref)!.limit -= e.amount;
      }
    }
    return b;
  }

  test("an outside replay of the log matches the provider and keeps the invariant", async () => {
    const events: BudgetEvent[] = [];
    let t = 0;
    const p = new HierarchicalBudgetProvider(() => t, (e) => events.push(e));
    p.open("job", 100_000n);
    const a = p.delegate("job", "a", 40_000n);
    const c = p.delegate("job", "c", 30_000n);
    if (!a.ok || !c.ok) throw new Error("delegation failed");
    const held = await Promise.all(Array.from({ length: 6 }, () => p.reserve(a.childRef, 10_000n, 1_000)));
    held.forEach((r, i) => {
      if (r.ok && i % 2 === 0) p.settle(r.reservation, 7_000n);
      else if (r.ok) p.release(r.reservation);
    });
    p.reserve(c.childRef, 5_000n, 1_000);
    t = 2_000; // the c hold expires
    p.revoke(c.childRef);
    p.remaining("job"); // triggers expiry bookkeeping

    // Sequence numbers are gapless and every refusal is on the record
    expect(events.map((e) => e.seq)).toEqual(events.map((_, i) => i + 1));
    expect(events.filter((e) => e.op === "refuse")).toHaveLength(2);

    const rebuilt = replay(events);
    for (const ref of ["job", a.childRef, c.childRef]) {
      const s = p.status(ref)!;
      const r = rebuilt.get(ref)!;
      expect([r.limit, r.consumed, r.reserved, r.delegated]).toEqual([s.limit, s.consumed, s.reserved, s.delegated]);
      expect(r.consumed + r.reserved + r.delegated <= r.limit).toBe(true);
    }
  });

  test("the log is optional", () => {
    const p = new HierarchicalBudgetProvider();
    p.open("job", 1n);
    expect(p.reserve("job", 1n).ok).toBe(true);
  });
});
