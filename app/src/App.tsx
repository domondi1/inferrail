import { LiveFeed } from "./screens/LiveFeed";
import { hasToken } from "./api";

// Hash-based routing only (docs/adr/0017) -- the server never needs a SPA
// catch-all route. Only "live" is implemented in this unit; the rest are
// real MISSION.md v0.4.0 screens, wired as disabled tabs so the nav
// doesn't have to be rebuilt as each one lands.
const TABS = [
  { hash: "#/live", label: "Live Feed", enabled: true },
  { hash: "#/work", label: "Work", enabled: false },
  { hash: "#/budgets", label: "Budgets", enabled: false },
  { hash: "#/recover", label: "Recover", enabled: false },
  { hash: "#/connect", label: "Connect", enabled: false },
  { hash: "#/settings", label: "Settings", enabled: false },
] as const;

export function App(): JSX.Element {
  return (
    <div className="app">
      <header className="masthead">
        <span className="wordmark">Inferrail</span>
        <nav className="nav">
          {TABS.map((tab) => (
            <button
              key={tab.hash}
              className="nav-tab"
              disabled={!tab.enabled}
              aria-current={tab.enabled ? "page" : undefined}
              title={tab.enabled ? undefined : "not built yet"}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </header>
      <main>
        {!hasToken() && (
          <p className="token-warning">
            No local API token found in this page's URL. Open the dashboard using the exact link
            printed by <code>inferrail serve --app-mode</code> (it includes{" "}
            <code>?token=...</code>) — the live feed below will stay disconnected without it.
          </p>
        )}
        <LiveFeed />
      </main>
    </div>
  );
}
