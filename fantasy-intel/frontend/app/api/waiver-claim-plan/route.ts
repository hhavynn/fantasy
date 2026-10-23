import { headers } from "next/headers";
import { api } from "@/lib/api";
import { claimPlanRouteResponse } from "@/lib/api-routes";
import { LEAGUE_HEADER } from "@/lib/league-scope";

/**
 * Checks the Waivers claim plan (issue #180), which lives in the browser. The browser cannot reach the backend
 * (API_URL is server-only), so this proxies `GET /api/waivers/claim-plan` through `api()`. It answers only under a
 * league (`/l/<lg>/api/waiver-claim-plan`, where proxy.ts sets the league header; it strips any header a client sends
 * itself), so a plan is never checked against another league's or the classic site's data; the backend authorizes the
 * league. Only `team_id` and well-formed claims are forwarded, and backend errors are not passed through verbatim
 * (lib/api-routes.ts, lib/route-error.ts). GET only: nothing is submitted anywhere.
 */
export async function GET(request: Request) {
  const league = (await headers()).get(LEAGUE_HEADER);
  return claimPlanRouteResponse(league, new URL(request.url).searchParams, (path) => api(path));
}
