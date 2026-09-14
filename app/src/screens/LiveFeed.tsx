import { useEffect, useRef, useState } from "react";
import { streamReceipts, type Receipt } from "../api";
import { attrSummary, formatCost, formatTime } from "../format";

const MAX_ROWS = 200;

export function LiveFeed(): JSX.Element {
  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const [status, setStatus] = useState<"connecting" | "connected" | "error">("connecting");
  const seen = useRef<Set<string>>(new Set());

  useEffect(() => {
    const close = streamReceipts(
      (receipt) => {
        // The stream can, in principle, redeliver a receipt already seen
        // (poll-based tail over `since`, docs/adr/0016) -- de-dupe on the
        // one field guaranteed unique rather than trust delivery-once.
        if (seen.current.has(receipt.receipt_id)) return;
        seen.current.add(receipt.receipt_id);
        setReceipts((prev) => [receipt, ...prev].slice(0, MAX_ROWS));
      },
      setStatus,
    );
    return close;
  }, []);

  return (
    <div>
      <h1 className="screen-title">Live Feed</h1>
      <p className="screen-subtitle">
        Every receipt this install has produced, newest first — printed live as each request
        completes.
      </p>
      <div className="status-line">
        <span className={`status-dot ${status}`} />
        {status === "connecting" && "Connecting"}
        {status === "connected" && "Connected"}
        {status === "error" && "Disconnected — retrying"}
      </div>
      {receipts.length === 0 ? (
        <p className="empty-state">
          No receipts yet. Send a request through the gateway (or run{" "}
          <code>inferrail ap demo</code>) and it will appear here immediately.
        </p>
      ) : (
        <div className="receipt-list">
          {receipts.map((r) => {
            const cost = formatCost(r.estimated_cost_usd);
            return (
              <div key={r.receipt_id} className={`receipt-row status-${r.status}`}>
                <span className="receipt-time">{formatTime(r.timestamp)}</span>
                <span className="receipt-model">
                  {r.provider}/{r.model}
                  {attrSummary(r.attributes) && (
                    <span className="receipt-attrs"> — {attrSummary(r.attributes)}</span>
                  )}
                </span>
                <span className="receipt-attrs">{r.status}</span>
                <span className={`receipt-cost ${cost.unknown ? "unknown" : ""}`}>
                  {cost.text}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
