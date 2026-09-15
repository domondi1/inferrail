import { useEffect, useState } from "react";
import { type CatalogFreshness, downloadReceiptsExport, getPricingFreshness } from "../api";

function CatalogRow({ c }: { c: CatalogFreshness }): JSX.Element {
  return (
    <div className="receipt-row">
      <span className="receipt-time">{c.model_count} models</span>
      <span className="receipt-model">{c.name}</span>
      <span className="receipt-attrs">
        {c.oldest_verified_date ? `oldest verified ${c.oldest_verified_date}` : "no models"}
      </span>
      <span className={c.is_stale ? "receipt-cost" : "receipt-attrs"}>
        {c.age_days !== null ? `${c.age_days}d old` : "—"}
        {c.is_stale && " (stale)"}
      </span>
    </div>
  );
}

export function Settings(): JSX.Element {
  const [catalogs, setCatalogs] = useState<CatalogFreshness[] | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [pingEnabled, setPingEnabled] = useState(false);

  useEffect(() => {
    getPricingFreshness()
      .then(setCatalogs)
      .catch(() => setCatalogs([]));
  }, []);

  async function onExport(): Promise<void> {
    setExportError(null);
    try {
      await downloadReceiptsExport();
    } catch {
      setExportError("Export failed.");
    }
  }

  return (
    <div>
      <h1 className="screen-title">Settings</h1>

      <h2 className="screen-title" style={{ fontSize: 18 }}>
        Export
      </h2>
      <p className="screen-subtitle">
        Download every stored receipt as JSONL — the same shape{" "}
        <code>inferrail receipts export</code> produces.
      </p>
      <button className="nav-tab" onClick={() => void onExport()}>
        Download receipts.jsonl
      </button>
      {exportError && (
        <p style={{ color: "var(--stamp)", fontSize: 12, marginTop: 8 }}>{exportError}</p>
      )}

      <h2 className="screen-title" style={{ fontSize: 18, marginTop: 32 }}>
        Pricing catalog
      </h2>
      <p className="screen-subtitle">
        Every built-in price is hand-verified against the vendor's own pricing page and shipped in
        the installed package — never fetched live. "Stale" is a reporting threshold (90 days), not
        a claim the price is wrong.
      </p>
      {catalogs === null && <p className="empty-state">Loading…</p>}
      {catalogs && catalogs.length > 0 && (
        <div className="receipt-list">
          {catalogs.map((c) => (
            <CatalogRow key={c.name} c={c} />
          ))}
        </div>
      )}
      <p className="receipt-attrs" style={{ marginTop: 8 }}>
        To refresh: <code>pip install --upgrade inferrail</code>, or add an explicit{" "}
        <code>pricing:</code> override in <code>inferrail.yaml</code>.
      </p>

      <h2 className="screen-title" style={{ fontSize: 18, marginTop: 32 }}>
        Telemetry
      </h2>
      <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
        <input
          type="checkbox"
          checked={pingEnabled}
          onChange={(e) => setPingEnabled(e.target.checked)}
          disabled
        />
        Send an anonymous opt-in usage ping
      </label>
      <p className="receipt-attrs" style={{ marginTop: 4, maxWidth: "58ch" }}>
        Disabled — no telemetry-ping mechanism exists in this codebase yet, so there is nothing to
        opt into. This control is a placeholder for a future capability, not a working toggle;
        nothing is ever sent today, regardless of this checkbox.
      </p>
    </div>
  );
}
