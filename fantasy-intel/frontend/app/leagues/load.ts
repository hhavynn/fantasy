import "server-only";
import { apiRequest } from "@/lib/api";
import {
  NO_SIGNING_REASON, SIGNED_OUT_REASON, loadProblem, parseLeagues, parseProviders, parseTeams,
  type LeagueView, type LoadProblem, type ProviderInfo, type TeamOption,
} from "@/lib/leagues";
import { currentUserId, userAssertionHeaders } from "@/lib/user-assertion.server";

// Everything on /leagues comes from the backend's /v2 API. This runs on the server only; the identity
// header it builds is passed straight to apiRequest and never leaves this module's callers.

export type LeagueWithTeams = { league: LeagueView; teams: TeamOption[]; teamsProblem: LoadProblem | null };

export type LeaguesData = {
  providers: ProviderInfo[];
  providersProblem: LoadProblem | null;
  /** Why there is no league list: nobody signed in, or this server cannot identify the user. */
  signedOutReason: string | null;
  leagues: LeagueWithTeams[];
  leaguesProblem: LoadProblem | null;
};

export async function loadLeaguesData(): Promise<LeaguesData> {
  const providersRes = await apiRequest("/v2/providers");
  const providers = providersRes.ok ? parseProviders(providersRes.data) : [];
  const providersProblem = providersRes.ok ? null : loadProblem("providers", providersRes);

  const empty = { providers, providersProblem, leagues: [], leaguesProblem: null };
  const userId = await currentUserId();
  if (!userId) return { ...empty, signedOutReason: SIGNED_OUT_REASON };
  const headers = userAssertionHeaders(userId);
  if (!headers) return { ...empty, signedOutReason: NO_SIGNING_REASON };

  const leaguesRes = await apiRequest("/v2/leagues", { headers });
  if (!leaguesRes.ok) return { ...empty, signedOutReason: null, leaguesProblem: loadProblem("your leagues", leaguesRes) };

  const leagues = await Promise.all(parseLeagues(leaguesRes.data).map(async (league): Promise<LeagueWithTeams> => {
    // A provider that cannot list teams says so in missing_capabilities; do not ask it to.
    if (league.missing_capabilities.includes("teams")) return { league, teams: [], teamsProblem: null };
    const res = await apiRequest(`/v2/leagues/${encodeURIComponent(league.league_id)}/teams`, { headers });
    return res.ok
      ? { league, teams: parseTeams(res.data), teamsProblem: null }
      : { league, teams: [], teamsProblem: loadProblem("teams", res) };
  }));
  return { providers, providersProblem, signedOutReason: null, leagues, leaguesProblem: null };
}

/** The server's clock for relative times; a function so render code stays pure. */
export function serverNow(): number {
  return Date.now();
}
