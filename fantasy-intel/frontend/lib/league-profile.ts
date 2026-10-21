// What kind of league the page is showing (GET /api/league-profile): its provider, the projection source the backend
// uses when a request names none, and whether the provider publishes projections at all. Pure functions so node --test
// covers them; lib/league-profile.server.ts fetches it once per request.

import { normalizeProvider, type Provider } from "./labels.ts";

export type { Provider } from "./labels.ts";

export type LeagueProfile = {
  provider: Provider;
  /** The backend's `source` when a request sends none: ESPN's projections, or Fantasy Intel's own ("independent"). */
  defaultSource: "espn" | "independent";
  /** False when the provider has no projections of its own (Sleeper): every projection on the site is Ours. */
  providerProjections: boolean;
};

/** The classic site: an ESPN league with ESPN projections by default. Also the answer whenever the profile is unknown. */
export const ESPN_PROFILE: LeagueProfile = Object.freeze({ provider: "espn", defaultSource: "espn", providerProjections: true });
export const SLEEPER_PROFILE: LeagueProfile = Object.freeze({ provider: "sleeper", defaultSource: "independent", providerProjections: false });

/** The profile a provider implies when the endpoint itself is unreachable. */
export function profileForProvider(provider: unknown): LeagueProfile {
  return normalizeProvider(provider) === "sleeper" ? SLEEPER_PROFILE : ESPN_PROFILE;
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/**
 * The profile from the endpoint's payload. Missing or malformed fields fall back to what the provider implies, so a
 * partial answer never turns ESPN projections off for an ESPN league; a payload that is not an object is the ESPN profile.
 */
export function parseLeagueProfile(raw: unknown, fallbackProvider?: unknown): LeagueProfile {
  if (!isRecord(raw)) return profileForProvider(fallbackProvider);
  const base = profileForProvider(raw.provider ?? fallbackProvider);
  const providerProjections = typeof raw.provider_projections === "boolean" ? raw.provider_projections : base.providerProjections;
  const defaultSource = raw.default_source === "independent" || raw.default_source === "espn"
    ? raw.default_source
    : providerProjections ? "espn" : "independent";
  // ESPN numbers cannot be the default where the provider has none.
  return { provider: base.provider, providerProjections, defaultSource: providerProjections ? defaultSource : "independent" };
}
