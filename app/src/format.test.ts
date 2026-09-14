import { describe, expect, it } from "vitest";
import { attrSummary, formatCost, formatTime } from "./format";

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
