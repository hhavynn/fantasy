// League scope for the multi-league site (Phase 2): `/l/<lg_id>/<page>` serves the existing page for that league.
// Pure functions only (no Next imports) so node --test covers them; proxy.ts and lib/api.ts apply them.
//
//   /l/<lg>/<page>?q   -> rewrite to /<page>?q, header x-fi-league: <lg>, cookie fi_league=<lg> (last viewed)
//   /<page>?q + cookie -> redirect to /l/<cookie>/<page>?q   ("old URLs go to the viewer's last-viewed league")
//   /<page>?league=clear -> clear the cookie, serve the classic (unscoped) site
// Everything else (assets, Next internals, the /leagues connections page) passes through untouched.

export const LEAGUE_HEADER = "x-fi-league";
export const LEAGUE_COOKIE = "fi_league";
export const CLEAR_PARAM = "league";
const LEAGUE_ID = /^lg_[A-Za-z0-9_-]{16,64}$/;
const UNSCOPED_PREFIXES = ["/_next", "/api", "/leagues", "/favicon", "/icon", "/apple-icon", "/robots", "/sitemap"];

export function isLeagueId(value: unknown): value is string {
  return typeof value === "string" && LEAGUE_ID.test(value);
}

/** `/l/<lg>/rest` -> { league: lg, path: "/rest" }; anything else -> { league: null, path }. */
export function splitLeaguePath(pathname: string): { league: string | null; path: string } {
  const m = /^\/l\/([^/]+)(\/.*)?$/.exec(pathname);
  if (!m || !isLeagueId(m[1])) return { league: null, path: pathname };
  return { league: m[1], path: m[2] || "/" };
}

/** The app path without a league prefix (for nav highlighting and other path matching). */
export function appPath(pathname: string): string {
  return splitLeaguePath(pathname).path;
}

export function leaguePath(league: string, path: string): string {
  const p = path.startsWith("/") ? path : `/${path}`;
  return `/l/${league}${p === "/" ? "" : p}`;
}

/** Prefix a backend path with the league's /v2 mount. Only the legacy /api routes are league-scoped. */
export function scopeApiPath(path: string, league: string | null | undefined): string {
  if (!league || !isLeagueId(league) || !path.startsWith("/api/")) return path;
  return `/v2/leagues/${league}${path}`;
}

function unscoped(path: string): boolean {
  return UNSCOPED_PREFIXES.some((p) => path === p || path.startsWith(`${p}/`) || path.startsWith(`${p}.`))
    || /\.[A-Za-z0-9]{2,5}$/.test(path);
}

export type ProxyDecision =
  | { kind: "next" }
  | { kind: "rewrite"; path: string; league: string }
  | { kind: "redirect"; location: string }
  | { kind: "clear"; location: string };

export function proxyDecision(args: { enabled: boolean; pathname: string; search: string; cookie?: string | null })
  : ProxyDecision {
  const { enabled, pathname, search, cookie } = args;
  if (!enabled) return { kind: "next" };
  const { league, path } = splitLeaguePath(pathname);
  if (league) return { kind: "rewrite", path: `${path}${search}`, league };
  if (pathname.startsWith("/l/") || pathname === "/l") return { kind: "next" };   // malformed id: the normal 404
  if (unscoped(pathname)) return { kind: "next" };
  const params = new URLSearchParams(search);
  if (params.get(CLEAR_PARAM) === "clear") {
    params.delete(CLEAR_PARAM);
    const rest = params.toString();
    return { kind: "clear", location: `${pathname}${rest ? `?${rest}` : ""}` };
  }
  if (isLeagueId(cookie)) return { kind: "redirect", location: `${leaguePath(cookie, pathname)}${search}` };
  return { kind: "next" };
}
