/**
 * Hierarchical budget provider: a reference for pre-execution spend policy hooks
 * (for example an AgentKit / x402 `policyProvider`) that need sub-agent budgets.
 *
 * Delegation is just a reservation against the parent. For every budget ref:
 *
 *     consumed + active reservations + delegated child shares <= limit
 *
 * Every call that changes a balance runs synchronously, with no `await` between
 * the check and the update, so in a single JS process two concurrent callers
 * can't both take the last unit of the same budget. A child can only spend from
 * its own share, and siblings can't both delegate the parent's last unit.
 *
 * In-memory and single-process by design: it shows the shape. A durable,
 * multi-process version needs the same operations inside a database transaction
 * (see ../authority.py and hosted/a2a_economic_authority/core.py, which do this
 * with SQLite).
 */

export type BudgetRef = string;

export interface BudgetReservation {
  ref: string;
  budgetRef: BudgetRef;
  amountAtomic: bigint;
  expiresAt: number;
}

export type ReserveResult =
  | { ok: true; reservation: BudgetReservation }
  | { ok: false; reason: "insufficient_budget" | "revoked" | "unknown_budget"; remainingAtomic: bigint };

export type DelegateResult =
  | { ok: true; childRef: BudgetRef }
  | { ok: false; reason: "insufficient_budget" | "revoked" | "unknown_budget"; remainingAtomic: bigint };

interface Budget {
  ref: BudgetRef;
  parentRef?: BudgetRef;
  limit: bigint;
  consumed: bigint;
  reserved: bigint; // active reservations against this budget
  delegated: bigint; // shares currently held by active children
  revoked: boolean;
}

export class HierarchicalBudgetProvider {
  private budgets = new Map<BudgetRef, Budget>();
  private reservations = new Map<string, BudgetReservation>();
  private nextId = 0;

  constructor(private readonly now: () => number = () => Date.now()) {}

  /** Open a root budget (for example one job or one user). */
  open(ref: BudgetRef, limitAtomic: bigint): BudgetRef {
    if (this.budgets.has(ref)) throw new Error(`budget ${ref} already exists`);
    this.budgets.set(ref, { ref, limit: limitAtomic, consumed: 0n, reserved: 0n, delegated: 0n, revoked: false });
    return ref;
  }

  /** What a budget can still reserve or delegate right now. */
  remaining(ref: BudgetRef): bigint {
    const b = this.budgets.get(ref);
    if (!b || b.revoked) return 0n;
    return b.limit - b.consumed - b.reserved - b.delegated;
  }

  /** Carve a child share out of the parent's remaining budget. */
  delegate(parentRef: BudgetRef, childId: string, maxAtomic: bigint): DelegateResult {
    this.expireStale();
    const parent = this.budgets.get(parentRef);
    if (!parent) return { ok: false, reason: "unknown_budget", remainingAtomic: 0n };
    if (this.isRevoked(parentRef)) return { ok: false, reason: "revoked", remainingAtomic: 0n };
    const remaining = this.remaining(parentRef);
    if (maxAtomic <= 0n || maxAtomic > remaining) {
      return { ok: false, reason: "insufficient_budget", remainingAtomic: remaining };
    }
    const childRef = `${parentRef}/${childId}`;
    if (this.budgets.has(childRef)) throw new Error(`budget ${childRef} already exists`);
    parent.delegated += maxAtomic;
    this.budgets.set(childRef, {
      ref: childRef,
      parentRef,
      limit: maxAtomic,
      consumed: 0n,
      reserved: 0n,
      delegated: 0n,
      revoked: false,
    });
    return { ok: true, childRef };
  }

  /** Pre-execution hook: hold `amountAtomic` against this budget, or refuse. */
  reserve(budgetRef: BudgetRef, amountAtomic: bigint, ttlMs = 60_000): ReserveResult {
    this.expireStale();
    const b = this.budgets.get(budgetRef);
    if (!b) return { ok: false, reason: "unknown_budget", remainingAtomic: 0n };
    if (this.isRevoked(budgetRef)) return { ok: false, reason: "revoked", remainingAtomic: 0n };
    const remaining = this.remaining(budgetRef);
    if (amountAtomic < 0n || amountAtomic > remaining) {
      return { ok: false, reason: "insufficient_budget", remainingAtomic: remaining };
    }
    b.reserved += amountAtomic;
    const reservation: BudgetReservation = {
      ref: `r${++this.nextId}`,
      budgetRef,
      amountAtomic,
      expiresAt: this.now() + ttlMs,
    };
    this.reservations.set(reservation.ref, reservation);
    return { ok: true, reservation };
  }

  /** Post-execution hook: record what was actually spent (at most the reserved amount). */
  settle(reservation: BudgetReservation, actualAtomic: bigint): void {
    const held = this.reservations.get(reservation.ref);
    if (!held) return; // already settled, released, or expired
    this.reservations.delete(held.ref);
    const b = this.budgets.get(held.budgetRef);
    if (!b) return;
    b.reserved -= held.amountAtomic;
    const spent = actualAtomic < held.amountAtomic ? actualAtomic : held.amountAtomic;
    b.consumed += spent > 0n ? spent : 0n;
    this.returnUnspentToParent(b);
  }

  /** Failure / release hook: free the reservation without spending. */
  release(reservation: BudgetReservation): void {
    const held = this.reservations.get(reservation.ref);
    if (!held) return;
    this.reservations.delete(held.ref);
    const b = this.budgets.get(held.budgetRef);
    if (!b) return;
    b.reserved -= held.amountAtomic;
    this.returnUnspentToParent(b);
  }

  /**
   * Revoke a budget and its whole subtree. New reservations and delegations are
   * refused immediately. Reservations already in flight keep their hold until
   * they settle or are released, so revocation never loses track of committed
   * spend. Each revoked budget's unspent share returns to its parent as soon as
   * it has nothing in flight (immediately, or when its last reservation settles
   * or is released).
   */
  revoke(ref: BudgetRef): void {
    const b = this.budgets.get(ref);
    if (!b) return;
    for (const child of this.children(ref)) this.revoke(child.ref);
    b.revoked = true;
    this.returnUnspentToParent(b);
  }

  /** Spent, held and delegated amounts for one budget. */
  status(ref: BudgetRef): { limit: bigint; consumed: bigint; reserved: bigint; delegated: bigint; remaining: bigint; revoked: boolean } | undefined {
    const b = this.budgets.get(ref);
    if (!b) return undefined;
    return { limit: b.limit, consumed: b.consumed, reserved: b.reserved, delegated: b.delegated, remaining: this.remaining(ref), revoked: this.isRevoked(ref) };
  }

  private children(ref: BudgetRef): Budget[] {
    return [...this.budgets.values()].filter((b) => b.parentRef === ref);
  }

  private isRevoked(ref: BudgetRef): boolean {
    for (let b = this.budgets.get(ref); b; b = b.parentRef ? this.budgets.get(b.parentRef) : undefined) {
      if (b.revoked) return true;
    }
    return false;
  }

  /**
   * A revoked child with nothing in flight hands its unspent share back to its
   * parent. The spent part stays counted against the parent, because the child's
   * share shrinks to exactly what it consumed.
   */
  private returnUnspentToParent(child: Budget): void {
    if (!child.revoked || !child.parentRef || child.reserved > 0n || child.delegated > 0n) return;
    const parent = this.budgets.get(child.parentRef);
    if (!parent) return;
    const unspent = child.limit - child.consumed;
    if (unspent <= 0n) return;
    parent.delegated -= unspent;
    child.limit = child.consumed;
    this.returnUnspentToParent(parent); // the parent may itself be revoked and now idle
  }

  /** Reservations past their TTL can no longer be settled: release them. */
  private expireStale(): void {
    const t = this.now();
    for (const r of [...this.reservations.values()]) {
      if (r.expiresAt <= t) this.release(r);
    }
  }
}
