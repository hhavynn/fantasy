// Server-side timings of backend calls (issue #201). Off unless API_TIMING is set: "1" logs every call, a fraction
// such as "0.05" logs that share. A line names the method, the path *template* (league ids and numeric ids replaced,
// no query string), the status, the duration and the backend's own total from its Server-Timing header: never a
// league id, a name or a token.

/** The backend answers `X-FI-Degraded: stale-files` when it served a league's previous files (data may be older). */
export const DEGRADED_HEADER = "x-fi-degraded";

/**
 * The fixed words of the backend's route paths (app/api/*.py routers). Every other segment (a league id, a team or
 * player id, a player selector, an invite token, a UUID) becomes a placeholder, so a log line carries a template only.
 * A route added later and missing here just logs its new word as `{id}`: less detail, never a value.
 */
const ROUTE_WORDS = new Set([
  "admin", "api", "compare", "connect", "context", "credential", "data-quality", "defenses", "discover", "evaluate",
  "evidence", "forward", "health", "import", "independent", "injury-impact", "invites", "kickers", "league-profile",
  "league-rules", "leagues", "lineup-projection", "lineup-projections", "luck-lab", "managers", "matchup", "me",
  "movers", "my-team", "opportunity", "options", "overview", "performance", "perspective", "pin", "players",
  "playoff-race", "power-rankings", "projections", "promotion", "providers", "ranges", "rankings", "redeem", "replay",
  "research", "schedule-intelligence", "scouting", "search", "start-sit", "status", "summary", "sync", "teams",
  "trades", "transactions", "usage", "v2", "value", "waivers", "weekly-recap",
]);
const LEAGUE_ID = /^lg_[A-Za-z0-9_-]{16,64}$/;

export function apiPathTemplate(path: string): string {
  const bare = path.split(/[?#]/, 1)[0];
  return bare.split("/").map((seg, i) => {
    if (i === 0 || seg === "" || ROUTE_WORDS.has(seg)) return seg;
    return LEAGUE_ID.test(seg) ? "{league}" : "{id}";
  }).join("/");
}

/** The share of calls to log (0 = off). Anything that is not "1", "true" or a number in (0, 1] is off. */
export function timingRate(raw: string | undefined): number {
  const v = (raw ?? "").trim().toLowerCase();
  if (v === "1" || v === "true") return 1;
  const n = Number(v);
  return v !== "" && Number.isFinite(n) && n > 0 ? Math.min(1, n) : 0;
}

/** The backend's total (`app;dur=`) from a Server-Timing header, in milliseconds, or null. */
export function backendTotalMs(serverTiming: string | null | undefined): number | null {
  const m = /(?:^|,)\s*app;dur=([0-9]+(?:\.[0-9]+)?)/.exec(serverTiming ?? "");
  return m ? Number(m[1]) : null;
}

export function timingLine(method: string, path: string, status: number | "offline", ms: number,
                           serverTiming?: string | null): string {
  const backend = backendTotalMs(serverTiming);
  return `api ${method} ${apiPathTemplate(path)} ${status} ${Math.round(ms)}ms` +
    (backend === null ? "" : ` backend=${Math.round(backend)}ms`);
}

/** Log one call's line when sampling picks it. `rand` and `log` are injectable for tests. */
export function recordTiming(method: string, path: string, status: number | "offline", ms: number,
                             serverTiming?: string | null, rate: number = timingRate(process.env.API_TIMING),
                             rand: () => number = Math.random, log: (line: string) => void = console.log): void {
  if (rate > 0 && rand() < rate) log(timingLine(method, path, status, ms, serverTiming));
}
