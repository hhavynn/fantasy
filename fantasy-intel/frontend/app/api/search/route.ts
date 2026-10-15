import { api } from "@/lib/api";
import type { SearchPlayer } from "@/lib/palette";

type BackendPlayer = {
  espn_player_id: number;
  full_name: string | null;
  position: string | null;
  pro_team: string | null;
  fantasy_team: string | null;
};

const MAX_QUERY = 80;
const LIMIT = 8;

/**
 * Player search for the command palette. The browser cannot reach the backend (API_URL is
 * server-only), so this proxies `GET /api/players?q=` through `api()`, which attaches the hosted-mode
 * API key on the server, and returns only what the palette shows. The site password gate in
 * `proxy.ts` covers this route like every page.
 */
export async function GET(request: Request) {
  const q = (new URL(request.url).searchParams.get("q") ?? "").trim().slice(0, MAX_QUERY);
  if (!q) return Response.json({ players: [] satisfies SearchPlayer[] });
  const res = await api<BackendPlayer[]>(`/api/players?q=${encodeURIComponent(q)}&limit=${LIMIT}`);
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
