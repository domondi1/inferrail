import { Budgets } from "./screens/Budgets";
import { LiveFeed } from "./screens/LiveFeed";
import { Work } from "./screens/Work";
import { hasToken } from "./api";
import { navigateTo, useHashRoute } from "./useHashRoute";

// Hash-based routing only (docs/adr/0017) -- the server never needs a SPA
// catch-all route. Screens land here as they're built; the rest stay
// disabled tabs so the nav doesn't have to be rebuilt as each one lands.
const TABS = [
  { screen: "live", label: "Live Feed", enabled: true },
  { screen: "work", label: "Work", enabled: true },
  { screen: "budgets", label: "Budgets", enabled: true },
  { screen: "recover", label: "Recover", enabled: false },
  { screen: "connect", label: "Connect", enabled: false },
  { screen: "settings", label: "Settings", enabled: false },
] as const;

export function App(): JSX.Element {
  const route = useHashRoute();

  return (
    <div className="app">
      <header className="masthead">
        <span className="wordmark">Inferrail</span>
        <nav className="nav">
          {TABS.map((tab) => (
            <button
              key={tab.screen}
              className="nav-tab"
              disabled={!tab.enabled}
              aria-current={route.screen === tab.screen ? "page" : undefined}
              title={tab.enabled ? undefined : "not built yet"}
              onClick={() => tab.enabled && navigateTo(tab.screen)}
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
            <code>?token=...</code>) — the screens below will stay disconnected without it.
          </p>
        )}
        {route.screen === "work" && <Work workId={route.param} />}
        {route.screen === "budgets" && <Budgets />}
        {route.screen !== "work" && route.screen !== "budgets" && <LiveFeed />}
      </main>
    </div>
  );
}
