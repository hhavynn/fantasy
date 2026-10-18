// Weekly projection source: ESPN (default) or Fantasy Intel's independent model ("ours").
// The choice lives in a cookie so it follows the user across pages; the backend applies it when a
// request carries `source=independent` (Start/Sit, Matchup and the player forward view; trades, waivers, team
// value and the playoff race stay on ESPN).

export type ProjectionSource = "espn" | "ours";
export type ProjectionOrigin = "ours" | "ours_zero" | "espn";

export const PROJECTION_COOKIE = "fi_projection";

/** What the backend says the served model's approval does and does not establish (projection_source.governance). */
export type ProjectionGovernance = {
  production_version: string;
  fallback_version: string;
  served_version: string | null;
  is_fallback: boolean;
  production_approval: string;
  espn_superiority_established: boolean;
  start_sit_ready: boolean;
  production_evidence: string;
  status_note: string | null;
};

export type ProjectionSourceDetail = {
  source: "espn" | "independent";
  available: boolean;
  reason: string | null;
  run: {
    run_id: string; model_version: string; created_at: string;
    /** Weeks ahead of the first unplayed week the run was made for (0 = the validated next-week forecast). */
    forecast_horizon?: number; projected_from_week?: number | null; horizon_note?: string | null;
  } | null;
  model_status: Record<string, string | null> | null;
  counts: Record<ProjectionOrigin, number> | null;
  governance?: ProjectionGovernance | null;
};

export function parseProjectionSource(v: string | null | undefined): ProjectionSource {
  return v === "ours" ? "ours" : "espn";
}

/** Add the backend's `source` parameter to an API path; the ESPN default leaves the path unchanged. */
export function withSource(path: string, source: ProjectionSource): string {
  if (source !== "ours") return path;
  const sep = path.includes("?") ? (path.endsWith("?") || path.endsWith("&") ? "" : "&") : "?";
  return `${path}${sep}source=independent`;
}

/** A short note when the shown run was made further ahead than next week (horizon > 0), or null for the next-week forecast. */
export function horizonCaution(d: ProjectionSourceDetail | null | undefined): string | null {
  if (!showingOurs(d)) return null;
  const h = d?.run?.forecast_horizon ?? 0;
  if (h <= 0) return null;
  const from = d?.run?.projected_from_week;
  return `This is a rest-of-season forecast made ${h} ${h === 1 ? "week" : "weeks"} ahead${from ? ` (from week ${from})` : ""}; it refreshes once a newer run is in.`;
}

/** True when the page is showing independent numbers (a run existed for the week). */
export function showingOurs(d: ProjectionSourceDetail | null | undefined): boolean {
  return !!d && d.source === "independent" && d.available;
}

export const SWITCH_SCOPE =
  "Weekly projections on Home, Matchup and Start/Sit, and the week-by-week forward view on a player's page and Rankings. Trades, waivers, team value and the playoff race stay on ESPN.";

/** One sentence on which model the numbers come from. */
export function modelStatusLine(d: ProjectionSourceDetail): string {
  const g = d.governance;
  const version = d.run?.model_version ?? "Fantasy Intel's model";
  if (g?.is_fallback) return `Fallback model ${version} (no eligible ${g.production_version} run this week).`;
  return `Expected points from ${version}, Fantasy Intel's own projection model.`;
}
