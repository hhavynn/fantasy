import { headers } from "next/headers";
import { api } from "@/lib/api";
import { tradeScenariosRouteResponse } from "@/lib/api-routes";
import { LEAGUE_HEADER } from "@/lib/league-scope";

/**
 * Compares the Trade analyzer's saved scenarios (issue #183), which live in the browser. The browser cannot reach the
 * backend (API_URL is server-only), so this proxies `GET /api/trades/scenarios` through `api()`. It answers only under
 * a league (`/l/<lg>/api/trade-scenarios`, where proxy.ts sets the league header; it strips any header a client sends
 * itself), so scenarios are never compared against another league's or the classic site's data; the backend authorizes
 * the league. Only a well-formed scenario list and start week are forwarded, and backend errors are not passed through
 * verbatim (lib/api-routes.ts, compareRouteError). GET only: no trade is proposed or submitted.
 */
export async function GET(request: Request) {
  const league = (await headers()).get(LEAGUE_HEADER);
  return tradeScenariosRouteResponse(league, new URL(request.url).searchParams, (path) => api(path));
}
