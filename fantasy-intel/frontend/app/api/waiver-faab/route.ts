import { headers } from "next/headers";
import { api } from "@/lib/api";
import { faabRouteResponse } from "@/lib/faab-route";
import { LEAGUE_HEADER } from "@/lib/league-scope";

/**
 * FAAB bid guidance for the Waivers claim plan (issue #179), which lives in the browser. The browser cannot reach the
 * backend (API_URL is server-only), so this proxies `GET /api/waivers/faab` through `api()`. It answers only under a
 * league (`/l/<lg>/api/waiver-faab`, where proxy.ts sets the league header; it strips any header a client sends
 * itself), so guidance is never read from another league's or the classic site's data; the backend authorizes the
 * league. Only `team_id`, well-formed claims and the share are forwarded, and backend errors are not passed through
 * verbatim (lib/faab-route.ts). GET only: nothing is bid anywhere.
 */
export async function GET(request: Request) {
  const league = (await headers()).get(LEAGUE_HEADER);
  return faabRouteResponse(league, new URL(request.url).searchParams, (path) => api(path));
}
