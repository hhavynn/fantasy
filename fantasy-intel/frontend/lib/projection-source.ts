// Weekly projection source: ESPN (default) or Fantasy Intel's independent model ("ours").
// The choice lives in a cookie so it follows the user across pages; the backend applies it when a
// request carries `source=independent` (Start/Sit and Matchup; rest-of-season pages stay on ESPN).

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
  run: { run_id: string; model_version: string; created_at: string } | null;
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

/** True when the page is showing independent numbers (a run existed for the week). */
export function showingOurs(d: ProjectionSourceDetail | null | undefined): boolean {
  return !!d && d.source === "independent" && d.available;
}

export const SWITCH_SCOPE =
  "Weekly projections on Home, Matchup and Start/Sit. Rest-of-season pages (trades, waivers, team value, playoff race) stay on ESPN: the independent model projects one week at a time.";

/** One sentence on what the shown model is and is not, from the backend's governance (never stronger). */
export function modelStatusLine(d: ProjectionSourceDetail): string {
  const g = d.governance;
  const version = d.run?.model_version ?? "the independent model";
  if (g && g.served_version && !g.is_fallback && g.production_approval === "owner_override") {
    return `Expected points from ${version}, production by owner override: not shown to be better than ESPN, `
      + "its validation so far is historical research only, and no position is Start/Sit-ready.";
  }
  const s = d.model_status ?? {};
  const lead = g?.is_fallback ? `Fallback model ${version} (no eligible ${g.production_version} run this week)` : `Expected points from ${version}`;
  return `${lead}: QB ${s.QB ?? "unknown"}, RB ${s.RB ?? "unknown"}, WR ${s.WR ?? "unknown"}, TE ${s.TE ?? "unknown"} `
    + "(validated means against trailing-average baselines; nothing is validated against ESPN yet).";
}
