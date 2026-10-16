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
  title: string;
};

/** The "Ours" cell for one fantasy week; weeks outside the ours series (the fantasy playoffs) have none. */
export function oursCell(w: OursWeek | undefined): OursCell {
  if (!w) return { value: null, kind: "none", experimental: false, title: "Not projected: outside the regular season" };
  const experimental = w.experimental;
  switch (w.origin) {
    case "ours":
      return { value: w.value, kind: "ours", experimental, title: experimental ? "Experimental: not validated beyond the next week" : "Fantasy Intel projection" };
    case "ours_zero":
      return { value: w.value ?? 0, kind: "zero", experimental, title: "Our model projects 0: bye or unavailable" };
    case "espn":
      return { value: w.value, kind: "espn", experimental: false, title: "No independent estimate for this week: ESPN's projection" };
    default:
      return { value: null, kind: "bye", experimental: false, title: "Bye week" };
  }
}

/** Chart points per fantasy week: our numbers only. ESPN-fallback weeks and byes are gaps, never ESPN's value or 0. */
export function oursPoints(weeks: ForwardWeek[], ours: OursForward): (number | null)[] {
  const byWeek = new Map(ours.weeks.map((w) => [w.week, w] as const));
  return weeks.map((w) => {
    const o = byWeek.get(w.week);
    return isOursNumber(o) ? o!.value : null;
  });
}

export function oursByWeek(ours: OursForward): Map<number, OursWeek> {
  return new Map(ours.weeks.map((w) => [w.week, w] as const));
}

/** Caption for the total: why it is missing when it is, or what it covers. */
export function oursTotalNote(ours: OursForward): string {
  const span = `${ours.ros_weeks_available}/${ours.ros_weeks_expected} wk`;
  if (ours.ros_complete) return `${span} · experimental beyond next week`;
  const gaps = ours.missing_weeks.length ? `ESPN's number in wk ${ours.missing_weeks.join(", ")}` : "incomplete";
  return `${span} · ${gaps}`;
}
