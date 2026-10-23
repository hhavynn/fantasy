import { api } from "@/lib/api";
import { type SearchPlayer, searchPath } from "@/lib/palette";

type BackendPlayer = {
  espn_player_id: number;
  full_name: string | null;
  position: string | null;
  pro_team: string | null;
  fantasy_team: string | null;
};

/**
 * Player search for the command palette and the compare pickers. The browser cannot reach the backend (API_URL is
 * server-only), so this proxies `GET /api/players?q=` through `api()`, which attaches the hosted-mode API key on the
 * server and returns only what the pickers show. On a league page the browser fetches `/l/<lg>/api/search`
 * (leagueRouteHref); proxy.ts rewrites that here with the league header, and `api()` then calls
 * `/v2/leagues/<lg>/api/players` with the visitor's token, so the backend authorizes that league. A bare `/api/search`
 * is unscoped (the proxy strips any league header the browser sends) and answers for the classic league only. The
 * site password gate in `proxy.ts` covers this route like every page.
 */
export async function GET(request: Request) {
  const path = searchPath(new URL(request.url).searchParams);
  if (!path) return Response.json({ players: [] satisfies SearchPlayer[] });
  const res = await api<BackendPlayer[]>(path);
  if (!res.ok) {
    return Response.json({ players: [], error: res.kind ?? "unknown" }, { status: 502 });
  }
  const players: SearchPlayer[] = res.data
    .filter((p) => p.full_name)
    .map((p) => ({
      id: p.espn_player_id,
      name: p.full_name as string,
      position: p.position,
      team: p.pro_team,
      owner: p.fantasy_team,
    }));
  return Response.json({ players });
}
