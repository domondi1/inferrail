import { Budgets } from "./screens/Budgets";
import { Connect } from "./screens/Connect";
import { LiveFeed } from "./screens/LiveFeed";
import { Recover } from "./screens/Recover";
import { Settings } from "./screens/Settings";
import { Work } from "./screens/Work";
import { hasToken } from "./api";
import { navigateTo, useHashRoute } from "./useHashRoute";

// Hash-based routing only (docs/adr/0017) -- the server never needs a SPA
// catch-all route. All six MISSION.md v0.4.0 screens are now built.
const TABS = [
  { screen: "live", label: "Live Feed" },
  { screen: "work", label: "Work" },
  { screen: "budgets", label: "Budgets" },
  { screen: "recover", label: "Recover" },
  { screen: "connect", label: "Connect" },
  { screen: "settings", label: "Settings" },
] as const;

type Screen = (typeof TABS)[number]["screen"];

const SCREENS: Record<Screen, () => JSX.Element> = {
  live: LiveFeed,
  work: () => <Work workId={null} />,
  budgets: Budgets,
  recover: Recover,
  connect: Connect,
  settings: Settings,
};

export function App(): JSX.Element {
  const route = useHashRoute();
  const screen = (route.screen in SCREENS ? route.screen : "live") as Screen;
  const ActiveScreen = screen === "work" ? () => <Work workId={route.param} /> : SCREENS[screen];

  return (
    <div className="app">
      <header className="masthead">
        <span className="wordmark">Inferrail</span>
        <nav className="nav">
          {TABS.map((tab) => (
            <button
              key={tab.screen}
              className="nav-tab"
              aria-current={screen === tab.screen ? "page" : undefined}
              onClick={() => navigateTo(tab.screen)}
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
        <ActiveScreen />
      </main>
    </div>
  );
}
