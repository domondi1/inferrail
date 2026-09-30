import { useEffect, useState } from "react";
import {
  getWork,
  listBlockedReceipts,
  listBudgets,
  listBudgetSpend,
  listWork,
  LocalApiError,
  type Budget,
  type BudgetSpend,
  type WorkSummary,
} from "../api";
import {
  formatCost,
  formatTime,
  formatWorkCost,
  inferenceStatusText,
  isBudgetBlock,
  plural,
} from "../format";
import { navigateTo } from "../useHashRoute";

function WorkRow({ w, onOpen }: { w: WorkSummary; onOpen: (id: string) => void }): JSX.Element {
  const cost = formatWorkCost(w.known_attributed_inference_cost_usd, w.unknown_cost_count);
  return (
    <div
      className="receipt-row work-row"
      role="button"
      tabIndex={0}
      onClick={() => onOpen(w.work_id)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") onOpen(w.work_id);
      }}
    >
      <span className="receipt-time">{plural(w.receipt_count, "request", "requests")}</span>
      <span className="receipt-model">{w.work_id}</span>
      <span className="receipt-attrs">
        {inferenceStatusText(w.inference_status)}
        {w.outcome_status ? ` · ${w.outcome_status}` : ""}
      </span>
      <span className={`receipt-cost ${cost.hasUnknown ? "unknown" : ""}`}>{cost.text}</span>
    </div>
  );
}

function cost(w: WorkSummary): { text: string; hasUnknown: boolean } {
  return formatWorkCost(w.known_attributed_inference_cost_usd, w.unknown_cost_count);
}

function WorkDetail({ workId, onBack }: { workId: string; onBack: () => void }): JSX.Element {
  const [detail, setDetail] = useState<WorkSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [budget, setBudget] = useState<{ budget: Budget; spend?: BudgetSpend } | null>(null);
  const [blocked, setBlocked] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    setBudget(null);
    setBlocked(0);
    getWork(workId)
      .then((d) => !cancelled && setDetail(d))
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof LocalApiError && e.status === 404 ? "Not found" : "Failed to load");
      });
    // Budget context is optional: without --app-mode budgets, or on any
    // failure, the detail still renders from the work rollup alone.
    Promise.all([listBudgets(), listBudgetSpend(), listBlockedReceipts()])
      .then(([budgets, spend, blockedRows]) => {
        if (cancelled) return;
        const own = budgets.find(
          (b) => b.scope === "work_id" && b.scope_value === workId && b.window === "per_work",
        );
        if (own) {
          setBudget({ budget: own, spend: spend.find((s) => s.budget_id === own.budget_id) });
        }
        setBlocked(
          blockedRows.filter((r) => isBudgetBlock(r) && r.attributes.work_id === workId).length,
        );
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [workId]);

  return (
    <div>
      <button className="nav-tab" onClick={onBack} style={{ marginBottom: 16 }}>
        ← Back to Work
      </button>
      <h2 className="screen-title" style={{ fontSize: 20 }}>
        {workId}
      </h2>
      {error && <p className="empty-state">{error}</p>}
      {detail && (
        <div className="figures work-hero">
          <div className="figure">
            <span className="figure-label">Spent</span>
            <span className={`figure-value ${cost(detail).hasUnknown ? "unknown" : ""}`}>
              {cost(detail).text}
            </span>
          </div>
          {budget && (
            <div className="figure">
              <span className="figure-label">Budget</span>
              <span className="figure-value">{formatCost(budget.budget.limit_usd).text}</span>
            </div>
          )}
          {blocked > 0 && (
            <div className="figure">
              <span className="figure-label">Blocked by budget</span>
              <span className="figure-value stamp">{blocked}</span>
            </div>
          )}
        </div>
      )}
      {detail && (
        <div className="receipt-list">
          <div className="receipt-row">
            <span className="receipt-attrs">Receipts</span>
            <span>{detail.receipt_count}</span>
            <span />
            <span />
          </div>
          <div className="receipt-row">
            <span className="receipt-attrs">Cost</span>
            <span>
              {
                formatWorkCost(detail.known_attributed_inference_cost_usd, detail.unknown_cost_count)
                  .text
              }
            </span>
            <span />
            <span />
          </div>
          <div className="receipt-row">
            <span className="receipt-attrs">Status</span>
            <span>{inferenceStatusText(detail.inference_status)}</span>
            <span />
            <span />
          </div>
          <div className="receipt-row">
            <span className="receipt-attrs">Outcome</span>
            <span>{detail.outcome_status ?? "not recorded"}</span>
            <span />
            <span />
          </div>
          <div className="receipt-row">
            <span className="receipt-attrs">Started</span>
            <span>{detail.started_at ? formatTime(detail.started_at) : "—"}</span>
            <span />
            <span />
          </div>
          <div className="receipt-row">
            <span className="receipt-attrs">Ended</span>
            <span>{detail.ended_at ? formatTime(detail.ended_at) : "—"}</span>
            <span />
            <span />
          </div>
        </div>
      )}
    </div>
  );
}

export function Work({ workId }: { workId: string | null }): JSX.Element {
  const [items, setItems] = useState<WorkSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (workId) return;
    let cancelled = false;
    listWork()
      .then((w) => !cancelled && setItems(w))
      .catch(() => !cancelled && setError("Failed to load work rollups"));
    return () => {
      cancelled = true;
    };
  }, [workId]);

  if (workId) {
    return <WorkDetail workId={workId} onBack={() => navigateTo("work")} />;
  }

  return (
    <div>
      <h1 className="screen-title">Work</h1>
      <p className="screen-subtitle">
        What each work item cost: every request tagged with the same work_id, added up. Click a row
        for details.
      </p>
      {error && <p className="empty-state">{error}</p>}
      {items === null && !error && <p className="empty-state">Loading…</p>}
      {items && items.length === 0 && (
        <p className="empty-state">
          No work evidence yet. Attribute a request with a <code>work_id</code> and it will appear
          here.
        </p>
      )}
      {items && items.length > 0 && (
        <div className="receipt-list">
          {items.map((w) => (
            <WorkRow key={w.work_id} w={w} onOpen={(id) => navigateTo("work", id)} />
          ))}
        </div>
      )}
    </div>
  );
}
