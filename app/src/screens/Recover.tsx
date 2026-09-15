import { useEffect, useState } from "react";
import {
  type PendingReview,
  listPendingReviews,
  recordOutcome,
} from "../api";
import { formatCost } from "../format";

function ReviewRow({
  item,
  onResolved,
}: {
  item: PendingReview;
  onResolved: () => void;
}): JSX.Element {
  const [outcome, setOutcome] = useState("accepted");
  const [reviewCostUsd, setReviewCostUsd] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onApprove(): Promise<void> {
    setSubmitting(true);
    setError(null);
    try {
      await recordOutcome(item.work_id, {
        outcome,
        source: "dashboard",
        review_cost_usd: reviewCostUsd || null,
      });
      onResolved();
    } catch {
      setError("Could not record that outcome.");
      setSubmitting(false);
    }
  }

  return (
    <div className="receipt-row" style={{ alignItems: "start" }}>
      <span className="receipt-time">{item.failure_type}</span>
      <span className="receipt-model">
        {item.work_id}
        <div className="receipt-attrs" style={{ marginTop: 4 }}>
          {item.reason}
          {item.retry_status && ` — retry: ${item.retry_status}`}
          {item.validation_passed === false && " — validation failed"}
        </div>
      </span>
      <span className="receipt-attrs">
        {formatCost(item.sunk_cost_usd).text} sunk
        {item.retry_cost_usd && ` + ${formatCost(item.retry_cost_usd).text} retry`}
      </span>
      <span style={{ display: "flex", flexDirection: "column", gap: 4, alignItems: "flex-end" }}>
        <div style={{ display: "flex", gap: 4 }}>
          <select value={outcome} onChange={(e) => setOutcome(e.target.value)}>
            <option value="accepted">accepted</option>
            <option value="corrected">corrected</option>
            <option value="rejected">rejected</option>
          </select>
          <input
            placeholder="review cost"
            inputMode="decimal"
            value={reviewCostUsd}
            onChange={(e) => setReviewCostUsd(e.target.value)}
            style={{ width: 80 }}
          />
        </div>
        <button
          className="nav-tab"
          onClick={() => void onApprove()}
          disabled={submitting}
          style={{ fontSize: 10 }}
        >
          Record outcome
        </button>
        {error && <span style={{ color: "var(--stamp)", fontSize: 11 }}>{error}</span>}
      </span>
    </div>
  );
}

export function Recover(): JSX.Element {
  const [items, setItems] = useState<PendingReview[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function refresh(): Promise<void> {
    try {
      setItems(await listPendingReviews());
      setError(null);
    } catch {
      setError(
        "Failed to load the review queue — is an AP recovery store configured? " +
          "See the path 'inferrail serve --app-mode' printed on startup.",
      );
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  return (
    <div>
      <h1 className="screen-title">Recover</h1>
      <p className="screen-subtitle">
        Work_ids currently awaiting human review. Recording an outcome here does exactly what{" "}
        <code>inferrail ap outcome</code> does — closing the loop without composing a command.
      </p>
      {error && <p className="empty-state">{error}</p>}
      {items === null && !error && <p className="empty-state">Loading…</p>}
      {items && items.length === 0 && (
        <p className="empty-state">Nothing pending review right now.</p>
      )}
      {items && items.length > 0 && (
        <div className="receipt-list">
          {items.map((item) => (
            <ReviewRow key={item.work_id} item={item} onResolved={() => void refresh()} />
          ))}
        </div>
      )}
    </div>
  );
}
