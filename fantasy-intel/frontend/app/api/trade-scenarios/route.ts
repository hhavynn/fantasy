import { headers } from "next/headers";
import { api } from "@/lib/api";
import { LEAGUE_HEADER, isLeagueId } from "@/lib/league-scope";
import { compareRouteError, scenariosBackendPath, type ScenarioCompareResponse } from "@/lib/trade-scenarios";

/**
 * Compares the Trade analyzer's saved scenarios (issue #183), which live in the browser. The browser cannot reach the
 * backend (API_URL is server-only), so this proxies `GET /api/trades/scenarios` through `api()`. It answers only under
 * a league (`/l/<lg>/api/trade-scenarios`, where proxy.ts sets the league header; it strips any header a client sends
 * itself), so scenarios are never compared against another league's or the classic site's data; the backend authorizes
 * the league. Only a well-formed scenario list and start week are forwarded. GET only: no trade is proposed or submitted.
 */
export async function GET(request: Request) {
  const league = (await headers()).get(LEAGUE_HEADER);
  if (!isLeagueId(league)) return Response.json({ error: "Open a league to compare trade scenarios." }, { status: 404 });
  const path = scenariosBackendPath(new URL(request.url).searchParams);
  if (!path) return Response.json({ error: "The scenario list is not well formed." }, { status: 400 });
  const res = await api<ScenarioCompareResponse>(path);
  if (!res.ok) {
    const { body, status } = compareRouteError(res);
    return Response.json(body, { status });
  }
  // `stale`: the backend served this league's previous files; the page then says the data may be older.
  return Response.json({ ...res.data, scope: league, stale: res.stale === true } satisfies ScenarioCompareResponse,
    { headers: { "Cache-Control": "private, no-store" } });
}
