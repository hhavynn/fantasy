import { cookies } from "next/headers";
import { getLeagueProfile } from "./league-profile.server";
import { PROJECTION_COOKIE, effectiveProjectionSource, parseProjectionSource, type ProjectionSource } from "./projection-source";

/** The visitor's choice from the cookie, ESPN unless they switched, before the league is taken into account. */
export async function getProjectionChoice(): Promise<ProjectionSource> {
  return parseProjectionSource((await cookies()).get(PROJECTION_COOKIE)?.value);
}

/**
 * The projection source the page uses: the visitor's choice (cookie, ESPN unless they switched), or always Ours for a
 * league whose provider has no projections (Sleeper). Server components only.
 */
export async function getProjectionSource(): Promise<ProjectionSource> {
  const [choice, profile] = await Promise.all([getProjectionChoice(), getLeagueProfile()]);
  return effectiveProjectionSource(choice, profile);
}
