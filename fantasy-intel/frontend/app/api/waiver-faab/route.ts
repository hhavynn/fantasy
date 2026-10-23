import { headers } from "next/headers";
import { api } from "@/lib/api";
import { faabBackendPath, type FaabResponse } from "@/lib/faab";
import { LEAGUE_HEADER, isLeagueId } from "@/lib/league-scope";

/**
 * FAAB bid guidance for the Waivers claim plan (issue #179), which lives in the browser. The browser cannot reach the
 * backend (API_URL is server-only), so this proxies `GET /api/waivers/faab` through `api()`. It answers only under a
 * league (`/l/<lg>/api/waiver-faab`, where proxy.ts sets the league header; it strips any header a client sends
 * itself), so guidance is never read from another league's or the classic site's data; the backend authorizes the
 * league. Only `team_id`, well-formed claims and the share are forwarded. GET only: nothing is bid anywhere.
 */
export async function GET(request: Request) {
  const league = (await headers()).get(LEAGUE_HEADER);
  if (!isLeagueId(league)) return Response.json({ error: "Open a league to see FAAB guidance." }, { status: 404 });
  const path = faabBackendPath(new URL(request.url).searchParams);
  if (!path) return Response.json({ error: "The FAAB request is not well formed." }, { status: 400 });
  const res = await api<FaabResponse>(path);
  if (!res.ok) return Response.json({ error: res.error, kind: res.kind ?? "unknown" }, { status: res.status ?? 502 });
  // `stale`: the backend served this league's previous files; the page then says the budget may be older.
  return Response.json({ ...res.data, scope: league, stale: res.stale === true } satisfies FaabResponse, { headers: { "Cache-Control": "private, no-store" } });
}
