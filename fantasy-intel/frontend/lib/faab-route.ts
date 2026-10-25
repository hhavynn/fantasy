// The /api/waiver-faab route's answer (issue #179), kept apart from the route file so it can be tested without Next.
// Backend failures never reach the browser verbatim (lib/route-error.ts): only an HTTP 404, 409 or 422 from the backend
// (a team outside the league, a busy database, a refused plan) forwards its own message; a 401 says to sign in again,
// in fixed words. Every answer is private and never stored by a cache.

import type { ApiResult } from "./api.ts";
import { faabBackendPath, type FaabResponse } from "./faab.ts";
import { isLeagueId } from "./league-scope.ts";
import { privateJson, routeErrorResponse } from "./route-error.ts";

export const FAAB_UNAVAILABLE = "FAAB guidance is unavailable right now.";
export const FAAB_SIGN_IN = "Sign in again to see FAAB guidance for this league.";

export async function faabRouteResponse(
  league: string | null,
  params: URLSearchParams,
  call: (path: string) => Promise<ApiResult<FaabResponse>>,
): Promise<Response> {
  if (!isLeagueId(league)) return privateJson({ error: "Open a league to see FAAB guidance." }, 404);
  const path = faabBackendPath(params);
  if (!path) return privateJson({ error: "The FAAB request is not well formed." }, 400);
  const res = await call(path);
  if (!res.ok) return routeErrorResponse(res, FAAB_UNAVAILABLE, FAAB_SIGN_IN);
  // `stale`: the backend served this league's previous files; the page then says the budget may be older.
  return privateJson({ ...res.data, scope: league, stale: res.stale === true } satisfies FaabResponse);
}
