import "server-only";
import { cache } from "react";
import { api } from "./api";
import { ESPN_PROFILE, parseLeagueProfile, type LeagueProfile } from "./league-profile";
import { currentLeague } from "./league-scope.server";

/**
 * The current league's profile, fetched once per request. When the endpoint fails (an older backend, offline), a
 * league-scoped page falls back to the provider /v2/leagues/{id} names, and the classic site to ESPN: unchanged.
 */
export const getLeagueProfile = cache(async (): Promise<LeagueProfile> => {
  const [res, league] = await Promise.all([api<unknown>("/api/league-profile"), currentLeague()]);
  if (res.ok) return parseLeagueProfile(res.data, league?.provider);
  return league ? parseLeagueProfile(null, league.provider) : ESPN_PROFILE;
});
