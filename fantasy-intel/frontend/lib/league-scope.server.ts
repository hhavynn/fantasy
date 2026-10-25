import "server-only";
import { headers } from "next/headers";
import { cache } from "react";
import { apiRequest } from "./api";
import { normalizeProvider, type Provider } from "./labels";
import { LEAGUE_HEADER, isLeagueId } from "./league-scope";

export type ScopedLeague = { id: string; name: string; committedAt: string | null; provider: Provider };

/**
 * The league this page is scoped to (proxy.ts), with its name, provider and served data time, or null on the classic
 * site. Fetched once per request.
 */
export const currentLeague = cache(async (): Promise<ScopedLeague | null> => {
  const id = (await headers()).get(LEAGUE_HEADER);
  if (!isLeagueId(id)) return null;
  const res = await apiRequest<{ name: string | null; provider?: string | null; data: { committed_at: string } | null }>(`/v2/leagues/${id}`);
  if (!res.ok || !res.data) {
    return { id, name: res.ok || res.status !== 404 ? "League" : "League not found", committedAt: null, provider: "espn" };
  }
  return { id, name: res.data.name || "League", committedAt: res.data.data?.committed_at ?? null, provider: normalizeProvider(res.data.provider) };
});
