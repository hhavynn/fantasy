// Safe answers for the Next route handlers under app/api that proxy one backend call through `api()`. Kept apart from
// the route files (and free of Next and `@/` imports) so node's test runner covers them.
//
// A failed `api()` result never reaches the browser verbatim: in a local run api() puts the internal API_URL and a
// start-the-server hint in its offline and invalid-JSON errors, and a 5xx `detail` can carry a server trace. So only an
// HTTP 404, 409 or 422 from the backend (a team outside the league, a busy database, a refused request) forwards the
// backend's own message; a 401 says to sign in again, in fixed words; everything else (offline, unknown, invalid JSON,
// schema, 5xx) is the route's fixed "unavailable" message with status 502. Every answer, refusal or not, is private and
// never stored by a shared cache, since it carries one visitor's league data.
//
// Server actions (app/leagues/actions.ts, app/account/actions.ts) follow the same rule through `actionErrorMessage`:
// their flows also rely on the backend's own 400 (ESPN refused the cookies), 403 (not in the league, admins only,
// sign-ups closed) and 429 (daily limit) messages, so those are forwarded too, plus four exact fixed 503 sentences; a
// 401 and everything else get fixed words.

import type { ApiResult } from "./api.ts";

export const NO_STORE = { "Cache-Control": "private, no-store" } as const;
export const SIGN_IN_AGAIN = "Sign in again to continue.";
const FORWARDED = new Set([404, 409, 422]);
/** The statuses whose `detail` the backend writes for the visitor on the add-league, league, invite and account flows. */
const ACTION_FORWARDED = new Set([400, 403, 404, 409, 422, 429]);
/**
 * The add-league flow's fixed 503 sentences (backend app/api/connect.py), forwarded only on an exact match: other 5xx
 * text is never shown. The first says the deployment cannot take private leagues at all, so "try again later" is wrong.
 */
export const ACTION_503_MESSAGES: ReadonlySet<string> = new Set([
  "Private leagues are not available in this deployment.",
  "ESPN could not be reached. Try again in a few minutes.",
  "Sleeper leagues are temporarily unavailable.",
  "Sleeper could not be reached. Try again in a minute.",
]);

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

/**
 * The message a server action shows for a failed `apiRequest()`: the backend's own words for a 400, 403, 404, 409, 422
 * or 429 (and a 503 that is exactly one of ACTION_503_MESSAGES), `signIn` for a 401, and `unavailable` for everything
 * else (offline, invalid JSON, schema, other 5xx), so the internal API_URL, a start-the-server hint or a server trace
 * never reaches the page.
 */
export function actionErrorMessage(res: ApiFailure, unavailable: string, signIn: string = SIGN_IN_AGAIN): string {
  if (res.kind === "http" && res.status === 401) return signIn;
  if (res.kind === "http" && res.status !== undefined && ACTION_FORWARDED.has(res.status)) return res.error;
  if (res.kind === "http" && res.status === 503 && ACTION_503_MESSAGES.has(res.error)) return res.error;
  return unavailable;
}
