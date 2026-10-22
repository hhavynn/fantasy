// Server-side timings of backend calls (issue #201). Off unless API_TIMING is set: "1" logs every call, a fraction
// such as "0.05" logs that share. A line names the method, the path *template* (league ids and numeric ids replaced,
// no query string), the status, the duration and the backend's own total from its Server-Timing header: never a
// league id, a name or a token.

/** The backend answers `X-FI-Degraded: stale-files` when it served a league's previous files (data may be older). */
export const DEGRADED_HEADER = "x-fi-degraded";

const LEAGUE_SEGMENT = /\/lg_[A-Za-z0-9_-]{16,64}(?=\/|$)/g;
const NUMERIC_SEGMENT = /\/\d+(?=\/|$)/g;

export function apiPathTemplate(path: string): string {
  const bare = path.split(/[?#]/, 1)[0];
  return bare.replace(LEAGUE_SEGMENT, "/{league}").replace(NUMERIC_SEGMENT, "/{id}");
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
