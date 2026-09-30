// Pure formatting helpers, split out from screens/LiveFeed.tsx purely so
// they're unit-testable without a DOM/React test harness.

export function formatCost(cost: string | null): { text: string; unknown: boolean } {
  // `null` means genuinely unknown cost -- never rendered as "$0"
  // (docs/PRODUCT.md's honest-numbers rule, MISSION.md's non-negotiables).
  if (cost === null) {
    return { text: "unknown", unknown: true };
  }
  return { text: `$${Number(cost).toFixed(4)}`, unknown: false };
}

export function formatTime(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleTimeString();
}

export function attrSummary(attrs: Record<string, string>): string {
  const parts: string[] = [];
  if (attrs.work_id) parts.push(`work:${attrs.work_id}`);
  if (attrs.project) parts.push(`project:${attrs.project}`);
  return parts.join(" · ");
}

/** A burn-bar fraction, clamped to [0, 1] for rendering -- the raw
 * spent/limit ratio can exceed 1 (a "warn" budget can be over its limit
 * by design; even a "block" budget can be pushed over post-flight by a
 * real cost exceeding its pre-flight estimate), but the bar itself
 * should never render wider than its track. Callers that need to know
 * "is this actually over" should compare spent > limit directly, not
 * infer it from this fraction. */
export function burnFraction(spentUsd: string, limitUsd: string): number {
  const limit = Number(limitUsd);
  if (limit <= 0) return 0;
  return Math.min(1, Math.max(0, Number(spentUsd) / limit));
}

/** A work_id's cost, honestly: a known partial total plus how many
 * receipts contributed nothing knowable, never collapsed into one
 * number. Mirrors `formatCost`'s "unknown is never $0" rule at the
 * work-rollup level (docs/PRODUCT.md's honest-numbers rule). */
export function formatWorkCost(
  knownCostUsd: string | null,
  unknownCount: number,
): { text: string; hasUnknown: boolean } {
  const known = formatCost(knownCostUsd);
  if (unknownCount === 0) {
    return { text: known.text, hasUnknown: false };
  }
  const suffix = `+${unknownCount} unknown`;
  return {
    text: known.unknown ? suffix : `${known.text} (${suffix})`,
    hasUnknown: true,
  };
}

/** Plain-English budget wording for the dashboard, so a reader sees
 * "Work item budget" and "blocks requests over the limit" before the
 * underlying scope/window/mode identifiers. The identifiers themselves
 * stay visible in smaller type next to this wording. */
export function budgetName(scope: string, scopeValue: string | null): string {
  if (scope === "global") return "All requests";
  return scopeValue ?? scope;
}

export function budgetKind(scope: string, window: string, mode: string): string {
  const what =
    scope === "work_id" ? "Work item budget" : scope === "project" ? "Project budget" : "Budget";
  const when = window === "daily" ? ", per day" : window === "monthly" ? ", per month" : "";
  const how = mode === "block" ? "blocks requests over the limit" : "warns only, never blocks";
  return `${what}${when} · ${how}`;
}

/** How many receipts in a blocked-request log came from one budget. */
export function blockedCountFor(
  budgetId: string,
  blocked: { attributes: Record<string, string> }[],
): number {
  return blocked.filter((r) => r.attributes.budget_id === budgetId).length;
}

/** A receipt the budget refused before any provider was contacted. */
export function isBudgetBlock(r: { status: string; attributes: Record<string, string> }): boolean {
  return r.status === "error" && Boolean(r.attributes.budget_id);
}

export function inferenceStatusText(status: string): string {
  switch (status) {
    case "success":
      return "all calls succeeded";
    case "partial":
      return "some calls failed or were blocked";
    case "error":
      return "all calls failed or were blocked";
    default:
      return "unknown";
  }
}

export function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}
