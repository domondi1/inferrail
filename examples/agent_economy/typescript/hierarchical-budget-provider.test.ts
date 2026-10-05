import { describe, expect, test } from "bun:test";
import { HierarchicalBudgetProvider } from "./hierarchical-budget-provider.ts";

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
