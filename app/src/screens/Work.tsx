import { useEffect, useState } from "react";
import { getWork, listWork, LocalApiError, type WorkSummary } from "../api";
import { formatTime, formatWorkCost } from "../format";
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
      <span className="receipt-time">{w.receipt_count} receipts</span>
      <span className="receipt-model">{w.work_id}</span>
      <span className="receipt-attrs">
        {w.inference_status}
        {w.outcome_status ? ` · ${w.outcome_status}` : ""}
      </span>
      <span className={`receipt-cost ${cost.hasUnknown ? "unknown" : ""}`}>{cost.text}</span>
    </div>
  );
}

function WorkDetail({ workId, onBack }: { workId: string; onBack: () => void }): JSX.Element {
  const [detail, setDetail] = useState<WorkSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    getWork(workId)
      .then((d) => !cancelled && setDetail(d))
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof LocalApiError && e.status === 404 ? "Not found" : "Failed to load");
      });
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
            <span>{detail.inference_status}</span>
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
        Cost per work_id, derived from receipts and outcome declarations. Click a row to drill in.
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
