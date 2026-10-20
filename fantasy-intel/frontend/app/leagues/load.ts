import "server-only";
import { apiRequest } from "@/lib/api";
import { SIGNED_OUT_REASON, loadProblem, parseProviders, type LeagueView, type LoadProblem, type ProviderInfo, type TeamOption } from "@/lib/leagues";

// Everything on /leagues comes from the backend's /v2 API. This runs on the server only. There is no sign-in yet
// (Supabase Auth arrives with the league switcher), so only the public provider list is loaded; the league list and
// the connect flow come back once the server can forward a verified Supabase session.

export type LeagueWithTeams = { league: LeagueView; teams: TeamOption[]; teamsProblem: LoadProblem | null };

export type LeaguesData = {
  providers: ProviderInfo[];
  providersProblem: LoadProblem | null;
  /** Why there is no league list: nobody can sign in yet. */
  signedOutReason: string | null;
  leagues: LeagueWithTeams[];
  leaguesProblem: LoadProblem | null;
};

export async function loadLeaguesData(): Promise<LeaguesData> {
  const providersRes = await apiRequest("/v2/providers");
  const providers = providersRes.ok ? parseProviders(providersRes.data) : [];
  const providersProblem = providersRes.ok ? null : loadProblem("providers", providersRes);
  return { providers, providersProblem, signedOutReason: SIGNED_OUT_REASON, leagues: [], leaguesProblem: null };
}

/** The server's clock for relative times; a function so render code stays pure. */
export function serverNow(): number {
  return Date.now();
}
