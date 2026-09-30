import { describe, expect, it } from "vitest";
import {
  attrSummary,
  blockedCountFor,
  budgetKind,
  budgetName,
  burnFraction,
  formatCost,
  formatTime,
  formatWorkCost,
  inferenceStatusText,
  isBudgetBlock,
  plural,
} from "./format";

describe("formatCost", () => {
  it("renders a null cost as unknown, never as $0", () => {
    expect(formatCost(null)).toEqual({ text: "unknown", unknown: true });
  });

  it("renders a real cost to 4 decimal places", () => {
    expect(formatCost("0.0003")).toEqual({ text: "$0.0003", unknown: false });
  });

  it("renders a zero-but-known cost as $0.0000, distinct from unknown", () => {
    expect(formatCost("0")).toEqual({ text: "$0.0000", unknown: false });
  });
});

describe("formatTime", () => {
  it("formats a valid ISO timestamp", () => {
    expect(formatTime("2026-01-01T00:00:00Z")).not.toBe("2026-01-01T00:00:00Z");
  });

  it("falls back to the raw string for an unparseable timestamp", () => {
    expect(formatTime("not-a-date")).toBe("not-a-date");
  });
});

describe("burnFraction", () => {
  it("computes a plain fraction under the limit", () => {
    expect(burnFraction("2.5", "10")).toBeCloseTo(0.25);
  });

  it("clamps at 1 when spend exceeds the limit", () => {
    expect(burnFraction("15", "10")).toBe(1);
  });

  it("returns 0 for a non-positive limit rather than dividing by zero", () => {
    expect(burnFraction("5", "0")).toBe(0);
  });
});

describe("formatWorkCost", () => {
  it("renders a fully-known cost plainly when there are no unknowns", () => {
    expect(formatWorkCost("1.5000", 0)).toEqual({ text: "$1.5000", hasUnknown: false });
  });

  it("renders a fully-unknown work as just the unknown-count suffix", () => {
    expect(formatWorkCost(null, 3)).toEqual({ text: "+3 unknown", hasUnknown: true });
  });

  it("renders a partially-known work with both the known total and the count", () => {
    expect(formatWorkCost("0.5000", 2)).toEqual({
      text: "$0.5000 (+2 unknown)",
      hasUnknown: true,
    });
  });
});

describe("attrSummary", () => {
  it("returns empty when neither work_id nor project is present", () => {
    expect(attrSummary({})).toBe("");
  });

  it("joins work_id and project when both present", () => {
    expect(attrSummary({ work_id: "w1", project: "acme" })).toBe("work:w1 · project:acme");
  });

  it("includes only work_id when project is absent", () => {
    expect(attrSummary({ work_id: "w1" })).toBe("work:w1");
  });
});

describe("budget wording", () => {
  it("names a work budget by its work id and global as all requests", () => {
    expect(budgetName("work_id", "contract-review-42")).toBe("contract-review-42");
    expect(budgetName("global", null)).toBe("All requests");
  });

  it("describes scope, window and mode in plain words", () => {
    expect(budgetKind("work_id", "per_work", "block")).toBe(
      "Work item budget · blocks requests over the limit",
    );
    expect(budgetKind("global", "daily", "warn")).toBe(
      "Budget, per day · warns only, never blocks",
    );
  });

  it("counts blocked receipts per budget", () => {
    const rows = [
      { attributes: { budget_id: "a" } },
      { attributes: { budget_id: "b" } },
      { attributes: { budget_id: "a" } },
    ];
    expect(blockedCountFor("a", rows)).toBe(2);
    expect(blockedCountFor("c", rows)).toBe(0);
  });

  it("treats only budget-refused errors as blocks", () => {
    expect(isBudgetBlock({ status: "error", attributes: { budget_id: "a" } })).toBe(true);
    expect(isBudgetBlock({ status: "error", attributes: {} })).toBe(false);
    expect(isBudgetBlock({ status: "success", attributes: { budget_id: "a" } })).toBe(false);
  });

  it("explains inference status and pluralizes counts", () => {
    expect(inferenceStatusText("partial")).toBe("some calls failed or were blocked");
    expect(plural(1, "request", "requests")).toBe("1 request");
    expect(plural(2, "request", "requests")).toBe("2 requests");
  });
});
