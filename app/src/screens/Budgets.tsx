import { useEffect, useState } from "react";
import {
  type Budget,
  type BudgetCreate,
  type BudgetMode,
  type BudgetScope,
  type BudgetSpend,
  type BudgetWindow,
  type Receipt,
  createBudget,
  deleteBudget,
  listBlockedReceipts,
  listBudgetSpend,
  listBudgets,
} from "../api";
import {
  blockedCountFor,
  budgetKind,
  budgetName,
  burnFraction,
  formatCost,
  formatTime,
  plural,
} from "../format";

function windowsFor(scope: BudgetScope): BudgetWindow[] {
  return scope === "work_id" ? ["per_work", "daily", "monthly"] : ["daily", "monthly"];
}

function NewBudgetForm({ onCreated }: { onCreated: () => void }): JSX.Element {
  const [scope, setScope] = useState<BudgetScope>("global");
  const [scopeValue, setScopeValue] = useState("");
  const [window, setWindow] = useState<BudgetWindow>("daily");
  const [mode, setMode] = useState<BudgetMode>("block");
  const [limitUsd, setLimitUsd] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: React.FormEvent): Promise<void> {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    const payload: BudgetCreate = {
      scope,
      scope_value: scope === "global" ? null : scopeValue,
      window,
      mode,
      limit_usd: limitUsd,
    };
    try {
      await createBudget(payload);
      setScopeValue("");
      setLimitUsd("");
      onCreated();
    } catch {
      setError("Could not create that budget — check the scope/window combination and limit.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      onSubmit={(e) => {
        void onSubmit(e);
      }}
      style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", margin: "16px 0" }}
    >
      <select
        value={scope}
        onChange={(e) => {
          const next = e.target.value as BudgetScope;
          setScope(next);
          setWindow(windowsFor(next)[0]);
        }}
      >
        <option value="global">global</option>
        <option value="project">project</option>
        <option value="work_id">work_id</option>
      </select>
      {scope !== "global" && (
        <input
          placeholder={scope === "project" ? "project name" : "work_id"}
          value={scopeValue}
          onChange={(e) => setScopeValue(e.target.value)}
          required
        />
      )}
      <select value={window} onChange={(e) => setWindow(e.target.value as BudgetWindow)}>
        {windowsFor(scope).map((w) => (
          <option key={w} value={w}>
            {w}
          </option>
        ))}
      </select>
      <select value={mode} onChange={(e) => setMode(e.target.value as BudgetMode)}>
        <option value="block">block</option>
        <option value="warn">warn</option>
      </select>
      <input
        placeholder="limit USD"
        inputMode="decimal"
        value={limitUsd}
        onChange={(e) => setLimitUsd(e.target.value)}
        style={{ width: 100 }}
        required
      />
      <button className="nav-tab" type="submit" disabled={submitting}>
        Add budget
      </button>
      {error && <span style={{ color: "var(--stamp)", fontSize: 12 }}>{error}</span>}
    </form>
  );
}

function BudgetRow({
  budget,
  spend,
  blockedCount,
  onDeleted,
}: {
  budget: Budget;
  spend: BudgetSpend | undefined;
  blockedCount: number;
  onDeleted: () => void;
}): JSX.Element {
  const fraction = spend ? burnFraction(spend.spent_usd, budget.limit_usd) : 0;
  const over = spend ? Number(spend.spent_usd) > Number(budget.limit_usd) : false;

  return (
    <div className="budget-card">
      <div className="budget-head">
        <span className="budget-name">{budgetName(budget.scope, budget.scope_value)}</span>
        <span className="budget-kind">{budgetKind(budget.scope, budget.window, budget.mode)}</span>
      </div>
      <div className="figures">
        <div className="figure">
          <span className="figure-label">Spent</span>
          <span className="figure-value">
            {spend ? formatCost(spend.spent_usd).text : "…"}
            {spend?.has_unpriced_usage && <span className="figure-note"> +unpriced</span>}
          </span>
        </div>
        <div className="figure">
          <span className="figure-label">Budget</span>
          <span className="figure-value">{formatCost(budget.limit_usd).text}</span>
        </div>
        <div className="figure">
          <span className="figure-label">Blocked</span>
          <span className={`figure-value ${blockedCount > 0 ? "stamp" : ""}`}>
            {plural(blockedCount, "request", "requests")}
          </span>
        </div>
      </div>
      <div className="budget-bar">
        <div className={over ? "over" : ""} style={{ width: `${fraction * 100}%` }} />
      </div>
      {blockedCount > 0 && budget.mode === "block" && (
        <p className="budget-note">
          {plural(blockedCount, "request was", "requests were")} stopped before reaching the
          provider, so {blockedCount === 1 ? "it was" : "they were"} never billed.
        </p>
      )}
      <div className="budget-meta">
        <span>{budget.budget_id}</span>
        <button
          className="nav-tab"
          onClick={() => onDeleted()}
          title="remove this budget"
          style={{ fontSize: 10 }}
        >
          Remove
        </button>
      </div>
    </div>
  );
}

function BlockedLog({ rows }: { rows: Receipt[] }): JSX.Element {
  if (rows.length === 0) {
    return (
      <p className="empty-state">
        No blocked requests yet. A blocking budget stops a request before it reaches the provider,
        and it shows up here.
      </p>
    );
  }
  return (
    <div className="receipt-list">
      {rows.map((r) => (
        <div key={r.receipt_id} className="receipt-row status-error" title={r.attributes.budget_id}>
          <span className="receipt-time">{formatTime(r.timestamp)}</span>
          <span className="receipt-model">
            {r.attributes.work_id ?? r.attributes.project ?? r.attributes.budget_id}
            <span className="receipt-attrs"> — over budget, not sent</span>
          </span>
          <span className="receipt-attrs">
            {r.provider}/{r.model}
          </span>
          <span className="receipt-cost unknown">blocked</span>
        </div>
      ))}
    </div>
  );
}

export function Budgets(): JSX.Element {
  const [budgets, setBudgets] = useState<Budget[] | null>(null);
  const [spend, setSpend] = useState<Map<string, BudgetSpend>>(new Map());
  const [blocked, setBlocked] = useState<Receipt[]>([]);
  const [error, setError] = useState<string | null>(null);

  async function refresh(): Promise<void> {
    try {
      const [b, s, blockedRows] = await Promise.all([
        listBudgets(),
        listBudgetSpend(),
        listBlockedReceipts(),
      ]);
      setBudgets(b);
      setSpend(new Map(s.map((entry) => [entry.budget_id, entry])));
      setBlocked(blockedRows);
      setError(null);
    } catch {
      setError("Failed to load budgets");
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function onDelete(budgetId: string): Promise<void> {
    await deleteBudget(budgetId);
    void refresh();
  }

  return (
    <div>
      <h1 className="screen-title">Budgets</h1>
      <p className="screen-subtitle">
        Spending limits, checked before every request. A blocking budget stops a request before it
        reaches the provider; a warning budget only records the overrun.
      </p>
      {error && <p className="empty-state">{error}</p>}
      {budgets === null && !error && <p className="empty-state">Loading…</p>}
      {budgets && budgets.length === 0 && (
        <p className="empty-state">No budgets yet. Add one at the bottom of this page, or send an <code>X-Inferrail-Budget-Usd</code> header with a work item's requests.</p>
      )}
      {budgets && budgets.length > 0 && (
        <div className="budget-list">
          {budgets.map((b) => (
            <BudgetRow
              key={b.budget_id}
              budget={b}
              spend={spend.get(b.budget_id)}
              blockedCount={blockedCountFor(b.budget_id, blocked)}
              onDeleted={() => void onDelete(b.budget_id)}
            />
          ))}
        </div>
      )}

      <h2 className="screen-title" style={{ fontSize: 20, marginTop: 32 }}>
        Blocked before reaching the provider
      </h2>
      <BlockedLog rows={blocked} />

      <h2 className="screen-title" style={{ fontSize: 20, marginTop: 32 }}>
        Add a budget
      </h2>
      <NewBudgetForm onCreated={() => void refresh()} />
    </div>
  );
}
