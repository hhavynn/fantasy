// The "ours" series of the player forward view: which weeks are our model's numbers, which fall back to
// ESPN, and what the chart and table show. Pure functions so the fallback marking is testable.

import type { ForwardProjection, ForwardWeek, OursForward, OursWeek } from "./types";

/** The ours block to render, only when the visitor asked for it and the backend sent it (null = ESPN view). */
export function oursBlock(f: ForwardProjection): OursForward | null {
  return f.ours ?? null;
}

/** True for a week whose number is our model's (a projection or its zero), not ESPN's. */
export function isOursNumber(w: OursWeek | undefined): boolean {
  return !!w && (w.origin === "ours" || w.origin === "ours_zero");
}

export type OursCell = {
  /** What to print; null = no number (gap). */
  value: number | null;
  /** ours: our projection; zero: our model says he can't score; espn: ESPN's number, marked; bye / none: nothing to show. */
  kind: "ours" | "zero" | "espn" | "bye" | "none";
  experimental: boolean;
  /** Short model tag ("ifp-1") when the number is from a model other than the production one; null otherwise. */
  model: string | null;
  title: string;
};

/** "ifp-1.0.0" -> "ifp-1": the badge text for a week shown from another model version. */
export function modelBadge(version: string | null, primary: string): string | null {
  if (!version || version === primary) return null;
  return version.replace(/\.\d+\.\d+$/, "");
}

/** The "Ours" cell for one fantasy week; weeks outside the ours series (the fantasy playoffs) have none. */
export function oursCell(w: OursWeek | undefined, primary = ""): OursCell {
  if (!w) return { value: null, kind: "none", experimental: false, model: null, title: "Not projected: outside the regular season" };
  const experimental = w.experimental;
  const model = primary ? modelBadge(w.model_version, primary) : null;
  switch (w.origin) {
    case "ours":
      return { value: w.value, kind: "ours", experimental, model, title: "Fantasy Intel projection" };
    case "ours_zero":
      return { value: w.value ?? 0, kind: "zero", experimental, model, title: "Our model projects 0: bye or unavailable" };
    case "espn":
      return { value: w.value, kind: "espn", experimental: false, model: null, title: "No independent estimate for this week: ESPN's projection" };
    default:
      return { value: null, kind: "bye", experimental: false, model: null, title: "Bye week" };
  }
}

/** Chart points per fantasy week: our numbers only. ESPN-fallback weeks and byes (even our model's 0 for one) are gaps. */
export function oursPoints(weeks: ForwardWeek[], ours: OursForward): (number | null)[] {
  const byWeek = new Map(ours.weeks.map((w) => [w.week, w] as const));
  return weeks.map((w) => {
    const o = byWeek.get(w.week);
    return isOursNumber(o) && o!.on_bye !== true ? o!.value : null;
  });
}

export function oursByWeek(ours: OursForward): Map<number, OursWeek> {
  return new Map(ours.weeks.map((w) => [w.week, w] as const));
}

/** Caption for the total: why it is missing when it is, or what it covers. Previous-model weeks and genuine ESPN
 * fallback weeks are named differently (both are in `missing_weeks`). `espnProjections` false (a Sleeper league):
 * there is no ESPN number to fall back to, so those weeks simply have no projection. */
export function oursTotalNote(ours: OursForward, espnProjections = true): string {
  const span = `${ours.ros_weeks_available}/${ours.ros_weeks_expected} wk`;
  if (ours.ros_complete) return span;
  const other = new Set(ours.other_model_weeks);
  const espn = ours.missing_weeks.filter((w) => !other.has(w));
  const parts: string[] = [];
  if (other.size) parts.push(`previous model in wk ${[...other].join(", ")}`);
  if (espn.length) parts.push(espnProjections ? `ESPN's number in wk ${espn.join(", ")}` : `no projection in wk ${espn.join(", ")}`);
  return `${span} · ${parts.length ? parts.join("; ") : "incomplete"}`;
}

/** The caution notice for the Ours series: why ESPN is shown instead (unavailable), or what is mixed, falls back
 * to ESPN or could not be verified while ours is shown. The title follows the actual fields, never a guess from
 * the reason being set. null = nothing to say. `espnProjections` false (a Sleeper league): nothing falls back to ESPN. */
export function oursNotice(ours: OursForward, espnProjections = true): { title: string; text: string } | null {
  if (!ours.available) {
    return {
      title: espnProjections ? "Showing ESPN projections" : "Fantasy Intel projections are not ready yet",
      text: ours.reason ?? "Fantasy Intel has no projections for the remaining weeks yet.",
    };
  }
  if (!ours.reason) return null;
  if (ours.mixed_models) return { title: "Mixed projection models", text: ours.reason };
  if (ours.other_model_weeks.length > 0) return { title: "Previous model in some weeks", text: ours.reason };
  if (ours.fallback_weeks.length > 0) return { title: espnProjections ? "Ours: some weeks use ESPN" : "Ours: some weeks have no projection", text: ours.reason };
  return { title: "Ours: see note", text: ours.reason };
}
