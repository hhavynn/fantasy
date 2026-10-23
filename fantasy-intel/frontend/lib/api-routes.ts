// The answers of the app/api route handlers for the claim plan, trade evidence and saved trade scenarios, kept apart
// from the route files so node's test runner covers them without Next (the FAAB route's is lib/faab-route.ts). Each
// takes the league header and the browser's query and calls the backend through `call` (the route passes `api()`).
// Every answer, refusal or not, is `private, no-store`, and a failed backend call never reaches the browser verbatim
// (lib/route-error.ts; the scenario comparison keeps its own mapping, `compareRouteError`).

import type { ApiResult } from "./api.ts";
import { claimPlanBackendPath, type ClaimPlanResponse } from "./claim-plan.ts";
import { isLeagueId } from "./league-scope.ts";
import { privateJson, routeErrorResponse } from "./route-error.ts";
import { evidencePath } from "./trade-discovery.ts";
import { compareRouteError, scenariosBackendPath, type ScenarioCompareResponse } from "./trade-scenarios.ts";
import type { DEvidence } from "./trade-types.ts";

type Call<T> = (path: string) => Promise<ApiResult<T>>;

export const CLAIM_PLAN_UNAVAILABLE = "The claim plan check is unavailable right now.";
export const CLAIM_PLAN_SIGN_IN = "Sign in again to check a claim plan for this league.";
export const EVIDENCE_UNAVAILABLE = "Trade evidence is unavailable right now.";
export const EVIDENCE_SIGN_IN = "Sign in again to see this trade's evidence.";

/** /api/waiver-claim-plan (issue #180): league pages only; only `team_id` and well-formed claims are forwarded. */
export async function claimPlanRouteResponse(league: string | null, params: URLSearchParams, call: Call<ClaimPlanResponse>): Promise<Response> {
  if (!isLeagueId(league)) return privateJson({ error: "Open a league to check a claim plan." }, 404);
  const path = claimPlanBackendPath(params);
  if (!path) return privateJson({ error: "The claim plan is not well formed." }, 400);
  const res = await call(path);
  if (!res.ok) return routeErrorResponse(res, CLAIM_PLAN_UNAVAILABLE, CLAIM_PLAN_SIGN_IN);
  // `stale`: the backend served this league's previous files; the page then never shows a claim as passing.
  return privateJson({ ...res.data, scope: league, stale: res.stale === true } satisfies ClaimPlanResponse);
}

/** /api/trade-evidence: one discovery row's evidence; only the discovery parameters and the row key are forwarded. */
export async function tradeEvidenceRouteResponse(params: URLSearchParams, call: Call<DEvidence>): Promise<Response> {
  const path = evidencePath(params);
  if (!path) return privateJson({ error: "A row key is required." }, 400);
  const res = await call(path);
  if (!res.ok) return routeErrorResponse(res, EVIDENCE_UNAVAILABLE, EVIDENCE_SIGN_IN);
  return privateJson(res.data);
}

/** /api/trade-scenarios (issue #183): league pages only; only a well-formed scenario list and start week are forwarded. */
export async function tradeScenariosRouteResponse(league: string | null, params: URLSearchParams, call: Call<ScenarioCompareResponse>): Promise<Response> {
  if (!isLeagueId(league)) return privateJson({ error: "Open a league to compare trade scenarios." }, 404);
  const path = scenariosBackendPath(params);
  if (!path) return privateJson({ error: "The scenario list is not well formed." }, 400);
  const res = await call(path);
  if (!res.ok) {
    const { body, status } = compareRouteError(res);
    return privateJson(body, status);
  }
  // `stale`: the backend served this league's previous files; the page then says the data may be older.
  return privateJson({ ...res.data, scope: league, stale: res.stale === true } satisfies ScenarioCompareResponse);
}
