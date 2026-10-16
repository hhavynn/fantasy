// Weekly projection source: ESPN (default) or Fantasy Intel's independent model ("ours").
// The choice lives in a cookie so it follows the user across pages; the backend applies it when a
// request carries `source=independent` (Start/Sit, Matchup and the player forward view; trades, waivers, team
// value and the playoff race stay on ESPN).

export type ProjectionSource = "espn" | "ours";
export type ProjectionOrigin = "ours" | "ours_zero" | "espn";

export const PROJECTION_COOKIE = "fi_projection";

export type ProjectionSourceDetail = {
  source: "espn" | "independent";
  available: boolean;
  reason: string | null;
  run: { run_id: string; model_version: string; created_at: string } | null;
  model_status: Record<string, string | null> | null;
  counts: Record<ProjectionOrigin, number> | null;
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

/** True when the page is showing independent numbers (a run existed for the week). */
export function showingOurs(d: ProjectionSourceDetail | null | undefined): boolean {
  return !!d && d.source === "independent" && d.available;
}

export const SWITCH_SCOPE =
  "Weekly projections on Home, Matchup and Start/Sit, and the week-by-week forward view on a player's page (experimental beyond next week). Trades, waivers, team value and the playoff race stay on ESPN.";
