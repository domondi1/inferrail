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
