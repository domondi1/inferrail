import { useEffect, useState } from "react";

/** Parses `#/screen/param` into `{ screen, param }` -- the one router this
 * dashboard needs (docs/adr/0017: hash-based routing, permanently, so the
 * server-side static mount never needs a SPA catch-all). Defaults to
 * "live" when the hash is empty or unrecognized, rather than a blank
 * screen. */
export interface Route {
  screen: string;
  param: string | null;
}

export function parseHash(hash: string): Route {
  const trimmed = hash.replace(/^#\/?/, "");
  const [screen, param] = trimmed.split("/");
  return { screen: screen || "live", param: param ? decodeURIComponent(param) : null };
}

export function useHashRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  return route;
}

export function navigateTo(screen: string, param?: string): void {
  window.location.hash = param ? `/${screen}/${encodeURIComponent(param)}` : `/${screen}`;
}
