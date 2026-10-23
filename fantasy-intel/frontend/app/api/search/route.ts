import { api } from "@/lib/api";
import { type SearchBackendPlayer, searchPath, searchResponse } from "@/lib/palette";

/**
 * Player search for the command palette and the compare pickers. The browser cannot reach the backend (API_URL is
 * server-only), so this proxies `GET /api/players?q=` through `api()`, which attaches the hosted-mode API key on the
 * server and returns only what the pickers show. On a league page the browser fetches `/l/<lg>/api/search`
 * (leagueRouteHref); proxy.ts rewrites that here with the league header, and `api()` then calls
 * `/v2/leagues/<lg>/api/players` with the visitor's token, so the backend authorizes that league. A bare `/api/search`
 * is unscoped (the proxy strips any league header the browser sends) and answers for the classic league only. Every
 * response is `private, no-store` (searchResponse). The site password gate in `proxy.ts` covers this route like every
 * page.
 */
export async function GET(request: Request) {
  const path = searchPath(new URL(request.url).searchParams);
  return searchResponse(path ? await api<SearchBackendPlayer[]>(path) : null);
}
