"""The operator report for Inferrail Hosted: adoption funnel, usage, purchases, money and
liabilities, computed from the ledger. Payload-free: it reads ids, timestamps, counts and
amounts, never request content.

Internal traffic is excluded by the caller. Pass the founder's, test and synthetic workspaces
and test wallets with labels. Every count below is for **external** traffic unless it says
otherwise, and the excluded totals are reported separately so nothing disappears silently.

**Accounting rule.**
- A credit purchase is cash received in advance, not revenue.
- Revenue is recognized when a prepaid credit is consumed by a billed run, at that workspace's
  average net price per credit.
- Unconsumed credits are deferred revenue: a liability, valued conservatively at the highest
  price per run the workspace paid.
- Contribution margin is only computed when costs are supplied. Unknown costs are reported as
  unknown, never as zero.
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from fractions import Fraction
from typing import Any

from workspaces import WorkspaceLedger

DAY = 86_400
# What a contribution margin needs and the ledger can't know. Reported until costs are supplied.
COST_INPUTS_NEEDED = [
    "hosting: the inferrail-cost-gateway service (instance plus 1 GB disk) is shared with the "
    "trial API; Hosted's share needs an allocation rule",
    "x402 facilitator fees per verify/settle at the facilitator's current pricing",
    "fees for sending refunds back on-chain (gas, if paid by the receiving wallet)",
    "card processing fees, if a card rail is ever enabled",
    "model spend: none; customers bring their own provider keys",
]
LIMITATIONS = [
    "Identity: a workspace is not a customer. One customer can create several workspaces, and "
    "one x402 wallet can serve several agents. Card buyers are counted per workspace.",
    "Unpaid payment challenges are counted only since usage_events existed, and per workspace "
    "only when the caller sent a valid key.",
    "Budget refusals and exhausted-allowance refusals are counted only since usage_events existed.",
    "Retention counts a run in the window where it first appeared. Later calls in an older "
    "run don't count as new activity.",
    "Timestamps are the service clock (UTC).",
]


def parse_labels(raw: str) -> dict[str, str]:
    """`"ws_a:test,ws_b:founder,ws_c"` → `{"ws_a": "test", "ws_b": "founder", "ws_c": "internal"}`.
    Wallet addresses are lower-cased."""
    out: dict[str, str] = {}
    for item in filter(None, (p.strip() for p in raw.split(","))):
        key, _, label = item.partition(":")
        key = key.strip()
        out[key.lower() if key.startswith("0x") else key] = label.strip() or "internal"
    return out


def _cents(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)  # round up: never understate


def _hours(seconds: list[float]) -> dict[str, float] | None:
    if not seconds:
        return None
    s = sorted(seconds)
    return {
        "median": round(statistics.median(s) / 3600, 2),
        "p90": round(s[min(len(s) - 1, int(0.9 * len(s)))] / 3600, 2),
        "n": len(s),
    }


def _table(ledger: WorkspaceLedger, sql: str) -> list[tuple[Any, ...]]:
    try:
        return list(ledger._db.execute(sql).fetchall())
    except Exception:  # a table from a newer schema isn't there yet
        return []


def build(
    ledger: WorkspaceLedger,
    *,
    exclude_workspaces: dict[str, str] | None = None,
    exclude_wallets: dict[str, str] | None = None,
    costs_cents: dict[str, int] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    ex_ws = exclude_workspaces or {}
    ex_wallet = {k.lower(): v for k, v in (exclude_wallets or {}).items()}
    with ledger._lock:  # one consistent snapshot; holds writers off for milliseconds
        workspaces = _table(ledger, "SELECT workspace_id, created_at FROM workspaces")
        runs = _table(
            ledger,
            "SELECT workspace_id, month, work_id, first_seen, paid, state FROM governed_runs",
        )
        grants = _table(
            ledger,
            "SELECT source, ref, workspace_id, runs, amount_usd_cents, granted_at "
            "FROM credit_grants",
        )
        reversals = _table(
            ledger,
            "SELECT source, ref, workspace_id, runs, amount_usd_cents FROM credit_reversals",
        )
        pending = _table(
            ledger,
            "SELECT source, ref, workspace_id, amount_usd_cents, authorizer, state "
            "FROM pending_purchases",
        )
        proofs = _table(ledger, "SELECT source, ref, amount_usd_cents FROM refund_proofs")
        events = _table(ledger, "SELECT workspace_id, kind, count FROM usage_events")
        outstanding_all = ledger.outstanding_liability_cents()

    def payer(source: str, ref: str) -> str | None:
        return ref.split(":", 1)[0].lower() if source == "x402" and ":" in ref else None

    def excluded_grant(source: str, ref: str, ws: str) -> bool:
        return ws in ex_ws or (payer(source, ref) or "") in ex_wallet

    created = {str(w): float(t) for w, t in workspaces}
    external = {w for w in created if w not in ex_ws}

    # -- workspaces and activation ------------------------------------------------------------
    billed_seen: dict[str, list[float]] = defaultdict(list)
    runs_by_month: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    paid_billed: dict[str, int] = defaultdict(int)
    for ws, month, _work, first_seen, paid, state in runs:
        if ws not in external:
            continue
        runs_by_month[ws][month] += 1
        if state == "billed":
            billed_seen[ws].append(float(first_seen))
            paid_billed[ws] += int(paid)
    activated = sorted(billed_seen)
    to_first = [min(billed_seen[w]) - created[w] for w in activated]
    days = {
        w: {time.strftime("%Y-%m-%d", time.gmtime(t)) for t in ts} for w, ts in billed_seen.items()
    }
    weeks = {
        w: {time.strftime("%G-W%V", time.gmtime(t)) for t in ts} for w, ts in billed_seen.items()
    }

    ev: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    anonymous_challenges = 0
    for ws, kind, count in events:
        if ws == "" and kind == "x402_challenge":
            anonymous_challenges += int(count)
        elif ws in external:
            ev[kind][ws] += int(count)

    def retention(n_days: int) -> dict[str, Any]:
        eligible = [w for w in activated if created[w] <= now - n_days * DAY]
        kept = [w for w in eligible if any(t >= created[w] + n_days * DAY for t in billed_seen[w])]
        return {
            "eligible_activated_workspaces": len(eligible),
            "retained": len(kept),
            "status": "measured" if eligible else "not enough elapsed time",
        }

    # -- purchases and money ------------------------------------------------------------------
    ext_grants = [g for g in grants if not excluded_grant(g[0], g[1], g[2])]
    grant_keys = {(g[0], g[1]) for g in ext_grants}
    ext_reversals = [r for r in reversals if (r[0], r[1]) in grant_keys]
    ext_proofs = [p for p in proofs if (p[0], p[1]) in grant_keys]
    ext_pending = [p for p in pending if p[2] not in ex_ws and str(p[4]).lower() not in ex_wallet]
    wallets: dict[str, int] = defaultdict(int)
    buyers: dict[str, int] = defaultdict(int)
    for source, ref, ws, *_ in ext_grants:
        buyers[ws] += 1
        if (w := payer(source, ref)) is not None:
            wallets[w] += 1

    gross = sum(int(g[4]) for g in ext_grants)
    refunded = sum(int(r[4]) for r in ext_reversals)
    x402_refunded = sum(int(r[4]) for r in ext_reversals if r[0] == "x402")
    verified = sum(int(p[2]) for p in ext_proofs)

    net_runs: dict[str, int] = defaultdict(int)
    net_cents: dict[str, int] = defaultdict(int)
    max_price: dict[str, Fraction] = {}
    for _s, _r, ws, n, cents, _t in ext_grants:
        net_runs[ws] += int(n)
        net_cents[ws] += int(cents)
        max_price[ws] = max(max_price.get(ws, Fraction(0)), Fraction(int(cents), int(n)))
    for _s, _r, ws, n, cents in ext_reversals:
        net_runs[ws] -= int(n)
        net_cents[ws] -= int(cents)
    recognized = Fraction(0)
    deferred = Fraction(0)
    for ws in net_runs:
        if net_runs[ws] <= 0:
            continue
        consumed = min(paid_billed.get(ws, 0), net_runs[ws])
        recognized += Fraction(consumed * net_cents[ws], net_runs[ws])
        deferred += (net_runs[ws] - consumed) * max_price[ws]
    pending_cents = sum(int(p[3]) for p in ext_pending if p[5] == "pending")
    recognized_cents = int(recognized)  # round down: never overstate revenue
    costs = costs_cents or {}

    excluded_labels: dict[str, int] = defaultdict(int)
    for w in created:
        if w in ex_ws:
            excluded_labels[ex_ws[w]] += 1

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "scope": "external traffic only, unless a field says otherwise",
        "accounting_rule": __doc__.split("**Accounting rule.**")[1].strip(),
        "excluded": {
            "workspaces_by_label": dict(excluded_labels),
            "wallets": len(ex_wallet),
            "grants": len(grants) - len(ext_grants),
        },
        "workspaces": {
            "external": len(external),
            "created_last_7_days": sum(1 for w in external if created[w] >= now - 7 * DAY),
            "created_last_30_days": sum(1 for w in external if created[w] >= now - 30 * DAY),
            "all_including_excluded": len(created),
        },
        "activation": {
            "workspaces_with_an_answered_run": len(activated),
            "time_to_first_answered_run_hours": _hours(to_first),
        },
        "funnel": _funnel(ev, activated),
        "usage": {
            "answered_runs": sum(len(v) for v in billed_seen.values()),
            "paid_answered_runs": sum(paid_billed.values()),
            "workspaces_used_on_2_plus_days": sum(1 for d in days.values() if len(d) >= 2),
            "workspaces_used_in_2_plus_weeks": sum(1 for d in weeks.values() if len(d) >= 2),
            "budget_refused_calls": sum(ev["budget_refused"].values()),
            "allowance_exhausted_refusals": sum(ev["allowance_exhausted"].values()),
            "workspaces_that_exhausted_the_free_allowance": len(
                set(ev["allowance_exhausted"])
                | {
                    w
                    for w, months in runs_by_month.items()
                    if any(n >= ledger.free_runs_per_month for n in months.values())
                }
            ),
        },
        "retention": {"day_7": retention(7), "day_30": retention(30)},
        "purchases": {
            "payment_challenges_served": sum(ev["x402_challenge"].values()),
            "payment_challenges_without_a_workspace_key": anonymous_challenges,
            "signed_x402_attempts": {
                state: sum(1 for p in ext_pending if p[5] == state)
                for state in ("pending", "settled", "failed")
            },
            "settled_purchases": len(ext_grants),
            "settled_purchases_by_source": {
                s: sum(1 for g in ext_grants if g[0] == s)
                for s in sorted({g[0] for g in ext_grants})
            },
            "distinct_paying_wallets": len(wallets),
            "distinct_paying_workspaces": len(buyers),
            "repeat_paying_wallets": sum(1 for n in wallets.values() if n >= 2),
            "repeat_paying_workspaces": sum(1 for n in buyers.values() if n >= 2),
            "repeat_payers": {
                "wallets": {w: n for w, n in sorted(wallets.items()) if n >= 2},
                "workspaces": {w: n for w, n in sorted(buyers.items()) if n >= 2},
            },
        },
        "money_cents": {
            "gross_sales": gross,
            "x402_usdc_settled": sum(int(g[4]) for g in ext_grants if g[0] == "x402"),
            "refunds_recorded": refunded,
            "x402_refunds_verified_on_chain": verified,
            "x402_refunds_recorded_without_chain_proof": max(x402_refunded - verified, 0),
            "net_cash_received": gross - refunded,
            "recognized_revenue": recognized_cents,
            "deferred_revenue_unspent_credits": _cents(deferred),
            "pending_purchases": pending_cents,
            "outstanding_liability_all_workspaces": outstanding_all,
            # What external customers could claim back if Hosted shut down today: their
            # unspent prepaid credits plus purchases still pending on-chain.
            "owed_to_external_customers_if_shut_down": _cents(deferred) + pending_cents,
        },
        "costs_cents": costs or "unknown: none supplied",
        "cost_inputs_needed": [] if costs else COST_INPUTS_NEEDED,
        "contribution_margin_cents": (recognized_cents - sum(costs.values())) if costs else None,
        "limitations": LIMITATIONS,
    }


def _funnel(ev: dict[str, dict[str, int]], activated: Any) -> dict[str, Any]:
    """Where external workspaces stop on the way to a first answered run. Counted from
    payload-free events (client class from the User-Agent, never content) since they existed."""

    def by_class(prefix: str) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for kind, per_ws in ev.items():
            if kind.startswith(prefix):
                out[kind[len(prefix) :]] += len(per_ws)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    called = {ws for kind, per_ws in ev.items() if kind.startswith("call_by:") for ws in per_ws}
    return {
        "workspaces_created_by_client": by_class("created_by:"),
        "workspaces_that_called": len(called),
        "workspaces_calling_by_client": by_class("call_by:"),
        "workspaces_that_sent_no_provider_key": len(ev["missing_provider_key"]),
        "workspaces_refused_for_an_unpriced_model": len(ev["unpriced_model"]),
        "workspaces_that_called_but_never_got_an_answer": len(called - set(activated)),
        "note": "since these events existed; a workspace is not a customer",
    }
