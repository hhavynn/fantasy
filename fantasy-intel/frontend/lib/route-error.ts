// Safe answers for the Next route handlers under app/api that proxy one backend call through `api()`. Kept apart from
// the route files (and free of Next and `@/` imports) so node's test runner covers them.
//
// A failed `api()` result never reaches the browser verbatim: api() puts the internal API_URL and a start-the-server
// hint in its offline and invalid-JSON errors, and a 5xx `detail` can carry a server trace. So only an HTTP 404, 409 or
// 422 from the backend (a team outside the league, a busy database, a refused request) forwards the backend's own
// message; a 401 says to sign in again, in fixed words; everything else (offline, unknown, invalid JSON, schema, 5xx) is
// the route's fixed "unavailable" message with status 502. Every answer, refusal or not, is private and never stored by
// a shared cache, since it carries one visitor's league data.

import type { ApiResult } from "./api.ts";

export const NO_STORE = { "Cache-Control": "private, no-store" } as const;
export const SIGN_IN_AGAIN = "Sign in again to continue.";
const FORWARDED = new Set([404, 409, 422]);

export type ApiFailure = Extract<ApiResult<unknown>, { ok: false }>;

/** A JSON answer that only the visitor's own browser may keep (refusals, errors and data alike). */
export function privateJson(body: unknown, status = 200): Response {
  return Response.json(body, { status, headers: NO_STORE });
}

/**
 * The browser's answer for a failed `api()` call. `unavailable` is the route's fixed message for everything the backend
 * did not write for the visitor; `signIn` replaces a 401's backend text (which can name the token's key id).
 */
export function routeErrorResponse(res: ApiFailure, unavailable: string, signIn: string = SIGN_IN_AGAIN): Response {
  if (res.kind === "http" && res.status === 401) return privateJson({ error: signIn }, 401);
  if (res.kind === "http" && res.status !== undefined && FORWARDED.has(res.status)) {
    return privateJson({ error: res.error, kind: "http" }, res.status);
  }
  return privateJson({ error: unavailable }, 502);
}
