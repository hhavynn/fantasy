import { headers } from "next/headers";
import { api } from "@/lib/api";
import { claimPlanBackendPath, type ClaimPlanResponse } from "@/lib/claim-plan";
import { LEAGUE_HEADER, isLeagueId } from "@/lib/league-scope";

/**
 * Checks the Waivers claim plan (issue #180), which lives in the browser. The browser cannot reach the backend
 * (API_URL is server-only), so this proxies `GET /api/waivers/claim-plan` through `api()`. It answers only under a
 * league (`/l/<lg>/api/waiver-claim-plan`, where proxy.ts sets the league header; it strips any header a client sends
 * itself), so a plan is never checked against another league's or the classic site's data; the backend authorizes the
 * league. Only `team_id` and well-formed claims are forwarded. GET only: nothing is submitted anywhere.
 */
export async function GET(request: Request) {
  const league = (await headers()).get(LEAGUE_HEADER);
  if (!isLeagueId(league)) return Response.json({ error: "Open a league to check a claim plan." }, { status: 404 });
  const path = claimPlanBackendPath(new URL(request.url).searchParams);
  if (!path) return Response.json({ error: "The claim plan is not well formed." }, { status: 400 });
  const res = await api<ClaimPlanResponse>(path);
  if (!res.ok) return Response.json({ error: res.error, kind: res.kind ?? "unknown" }, { status: res.status ?? 502 });
  // `stale`: the backend served this league's previous files; the page then never shows a claim as passing.
  return Response.json({ ...res.data, scope: league, stale: res.stale === true } satisfies ClaimPlanResponse, { headers: { "Cache-Control": "private, no-store" } });
}
