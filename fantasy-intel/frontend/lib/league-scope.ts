// League scope for the multi-league site (Phase 2): `/l/<lg_id>/<page>` serves the existing page for that league.
// Pure functions only (no Next imports) so node --test covers them; proxy.ts and lib/api.ts apply them.
//
//   /l/<lg>/<page>?q   -> rewrite to /<page>?q, header x-fi-league: <lg>, cookie fi_league=<lg> (last viewed);
//                         with `league=clear` in q, a redirect to the same league page without it (it means something
//                         only on unscoped URLs)
//   /<page>?q + cookie -> redirect to /l/<cookie>/<page>?q   ("old URLs go to the viewer's last-viewed league")
//   /<page>?league=clear -> clear the cookie, serve the classic (unscoped) site
// Everything else (assets, Next internals, the /leagues connections page, sign-in, invites, terms and privacy) passes
// through untouched.

export const LEAGUE_HEADER = "x-fi-league";
export const LEAGUE_COOKIE = "fi_league";
export const CLEAR_PARAM = "league";
const LEAGUE_ID = /^lg_[A-Za-z0-9_-]{16,64}$/;
// Account pages (sign-in, its email callback, invites) and the terms and privacy policy belong to no league: never
// rewritten or redirected.
export const UNSCOPED_PREFIXES = ["/_next", "/api", "/leagues", "/login", "/auth", "/invite", "/account", "/admin", "/terms", "/privacy", "/favicon",
  "/icon", "/apple-icon", "/robots", "/sitemap"];

export function isLeagueId(value: unknown): value is string {
  return typeof value === "string" && LEAGUE_ID.test(value);
}

/**
 * `/l/<lg>/rest` -> { league: lg, path: "/rest" }; anything else -> { league: null, path }. A rest that starts with `//`
 * or holds a backslash is not a league path: proxy.ts resolves the rest against the request URL, where `//evil.com` (and
 * `/\evil.com`, which URL parsers read as `//`) would name another host.
 */
export function splitLeaguePath(pathname: string): { league: string | null; path: string } {
  const m = /^\/l\/([^/]+)(\/.*)?$/.exec(pathname);
  if (!m || !isLeagueId(m[1]) || m[2]?.startsWith("//") || m[2]?.includes("\\")) return { league: null, path: pathname };
  return { league: m[1], path: m[2] || "/" };
}

/**
 * True when a client-side navigation from `previous` to `pathname` changes the league scope: into a league, out of
 * one (/leagues, /terms), or to another league. The root layout is kept across client navigations, so its header
 * (league switcher, sync state, projection switch) must be re-rendered then (components/ScopeRefresh.tsx).
 */
export function crossesLeagueScope(previous: string | null, pathname: string): boolean {
  return previous !== null && splitLeaguePath(previous).league !== splitLeaguePath(pathname).league;
}

/** The app path without a league prefix (for nav highlighting and other path matching). */
export function appPath(pathname: string): string {
  return splitLeaguePath(pathname).path;
}

export function leaguePath(league: string, path: string): string {
  const p = path.startsWith("/") ? path : `/${path}`;
  return `/l/${league}${p === "/" ? "" : p}`;
}

/**
 * An in-app href for the current league: `/waivers?team=2` -> `/l/<lg>/waivers?team=2`, so a click goes straight to
 * the league page instead of through the proxy's cookie redirect. Unscoped targets (the connections page, assets,
 * external links, `?league=clear`) and hrefs that already carry a league are returned unchanged.
 */
export function withLeague(href: string, league: string | null | undefined): string {
  if (!league || !isLeagueId(league) || !href.startsWith("/") || href.startsWith("//")) return href;
  // Path, then everything after it (query and hash) kept whole: a split with a limit would drop the hash after a query.
  const [, path, query] = /^([^?#]*)([\s\S]*)$/.exec(href)!;
  if (splitLeaguePath(path).league || unscoped(path) || new URLSearchParams(query.replace(/^[?]/, "").split("#")[0]).get(CLEAR_PARAM) === "clear") return href;
  return `${leaguePath(league, path)}${query}`;
}

/**
 * The href a link rendered on the page at `pathname` should carry (components/LeagueLink.tsx, TeamLink, and the
 * client navigations that push a server-built href): `withLeague` with `league` when given, else with the league the
 * page itself is in (`/l/<lg>/...`). So `/players?id=1` on `/l/<lg>/rankings` becomes `/l/<lg>/players?id=1` and the
 * click never depends on the last-viewed-league cookie. Identity on the classic site; external, relative (`?q`,
 * `#x`), unscoped and already-scoped hrefs are returned unchanged (`withLeague`).
 */
export function leagueLinkHref(href: string, pathname: string | null | undefined, league?: string | null): string {
  return withLeague(href, league || (pathname ? splitLeaguePath(pathname).league : null));
}

/**
 * A same-origin Next route handler (`/api/...` under app/api) fetched by a client component on a league page:
 * `/api/trade-evidence?k` -> `/l/<lg>/api/trade-evidence?k`. proxy.ts rewrites that with the league header, so the
 * handler's `api()` call reaches the league's /v2 mount; a bare `/api/...` fetch is unscoped (the proxy strips any league
 * header) and would answer for the classic league. Identity on the classic site and for anything but `/api/...`.
 */
export function leagueRouteHref(href: string, league: string | null | undefined): string {
  if (!league || !isLeagueId(league) || !/^\/api(\/|\?|$)/.test(href)) return href;
  return `/l/${league}${href}`;
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

/**
 * `?league=clear` means "back to the classic site" only on an unscoped URL. On `/l/<lg>/<page>` the browser is
 * redirected to the same league page without any `league` param: page link builders copy the page's params into their
 * hrefs (perspectiveHref on the server, and the Nav team picker and tab memory from the browser URL's
 * useSearchParams()), and `withLeague` leaves a `league=clear` href unscoped, so those links would leave the league
 * carrying its team id. The location is built only from the validated league id, the path and the remaining params.
 * Null when the query has no clear `league` value (the page is then rewritten with its query exactly as sent).
 */
function clearedLeagueLocation(league: string, path: string, search: string): string | null {
  const params = new URLSearchParams(search);
  if (!params.getAll(CLEAR_PARAM).includes("clear")) return null;
  params.delete(CLEAR_PARAM);
  const rest = params.toString();
  return `${leaguePath(league, path)}${rest ? `?${rest}` : ""}`;
}

export type ProxyDecision =
  | { kind: "next" }
  | { kind: "rewrite"; path: string; league: string }
  | { kind: "redirect"; location: string }
  | { kind: "clear"; location: string };

export function proxyDecision(args: {
  enabled: boolean; pathname: string; search: string; cookie?: string | null;
  /** CLASSIC_SITE_ENABLED: false once the single-league site is retired (Phase 5): an unscoped page then always goes to
   *  a league, the last one viewed or the leagues page. */
  classic?: boolean;
}): ProxyDecision {
  const { enabled, pathname, search, cookie, classic = true } = args;
  if (!enabled) return { kind: "next" };
  const { league, path } = splitLeaguePath(pathname);
  if (league) {
    const cleared = clearedLeagueLocation(league, path, search);
    return cleared ? { kind: "redirect", location: cleared } : { kind: "rewrite", path: `${path}${search}`, league };
  }
  if (pathname.startsWith("/l/") || pathname === "/l") return { kind: "next" };   // malformed id: the normal 404
  if (unscoped(pathname)) return { kind: "next" };
  const params = new URLSearchParams(search);
  if (params.get(CLEAR_PARAM) === "clear") {
    params.delete(CLEAR_PARAM);
    const rest = params.toString();
    return { kind: "clear", location: classic ? `${pathname}${rest ? `?${rest}` : ""}` : "/leagues" };
  }
  if (isLeagueId(cookie)) return { kind: "redirect", location: `${leaguePath(cookie, pathname)}${search}` };
  return classic ? { kind: "next" } : { kind: "redirect", location: "/leagues" };
}
