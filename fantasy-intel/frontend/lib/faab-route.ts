// The /api/waiver-faab route's answer (issue #179), kept apart from the route file so it can be tested without Next.
// Backend failures never reach the browser verbatim: api() puts the internal API_URL and a start-the-server hint in
// offline and unknown errors, so only an HTTP 404, 409 or 422 from the backend (a team outside the league, a busy
// database, a refused plan) forwards its own message. Every answer is private and never stored by a cache.

import type { ApiResult } from "./api.ts";
import { faabBackendPath, type FaabResponse } from "./faab.ts";
import { isLeagueId } from "./league-scope.ts";

export const FAAB_UNAVAILABLE = "FAAB guidance is unavailable right now.";
const NO_STORE = { "Cache-Control": "private, no-store" };
const FORWARDED = new Set([404, 409, 422]);

export async function faabRouteResponse(
  league: string | null,
  params: URLSearchParams,
  call: (path: string) => Promise<ApiResult<FaabResponse>>,
): Promise<Response> {
  if (!isLeagueId(league)) return Response.json({ error: "Open a league to see FAAB guidance." }, { status: 404, headers: NO_STORE });
  const path = faabBackendPath(params);
  if (!path) return Response.json({ error: "The FAAB request is not well formed." }, { status: 400, headers: NO_STORE });
  const res = await call(path);
  if (!res.ok) {
    const forward = res.kind === "http" && res.status !== undefined && FORWARDED.has(res.status);
    return forward
      ? Response.json({ error: res.error, kind: "http" }, { status: res.status, headers: NO_STORE })
      : Response.json({ error: FAAB_UNAVAILABLE }, { status: 502, headers: NO_STORE });
  }
  // `stale`: the backend served this league's previous files; the page then says the budget may be older.
  return Response.json({ ...res.data, scope: league, stale: res.stale === true } satisfies FaabResponse, { headers: NO_STORE });
}
