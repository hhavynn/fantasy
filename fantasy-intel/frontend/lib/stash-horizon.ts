/** Supported future-week windows of the backend's stash-plan/v1 contract. */
export const STASH_HORIZONS = [2, 4, 6] as const;
export type StashHorizon = (typeof STASH_HORIZONS)[number];

export function readStashHorizon(raw: string | string[] | undefined): StashHorizon {
  const first = Array.isArray(raw) ? raw[0] : raw;
  return first === "2" ? 2 : first === "6" ? 6 : 4;
}

/** A missing projected delta is unknown, not zero; signed positive and negative numbers are projected points. */
export function stashGain(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const rounded = value.toFixed(1);
  return `${value > 0 && Number(rounded) !== 0 ? "+" : ""}${rounded}`;
}

/** The stash-plan/v1 `counts` (backend analytics/stash_planner.py). Every key may be absent (`{}` with no later weeks). */
export type StashCounts = {
  considered?: number; evaluated?: number; excluded_unprojected?: number; excluded_position_limit?: number;
  no_possible_gain?: number; not_evaluated_cap?: number; unknown?: number; pick_up_now?: number; stashes?: number;
};

/**
 * Where every eligible free agent went, so the evaluated list is never read as the whole pool. The backend splits
 * `considered` into exactly evaluated + missing a projection + blocked by a position limit + no possible gain + beyond
 * the per-position cap; `unknown` is the evaluated ones whose value could not be computed. Null when nothing was compared.
 */
export function stashCoverage(c: StashCounts): string | null {
  if (c.considered === undefined) return null;
  const parts: string[] = [];
  if (c.evaluated) {
    parts.push(`${c.evaluated} evaluated exactly${c.unknown ? ` (${c.unknown} with an unknown value: a projection is missing)` : ""}`);
  }
  const add = (n: number | undefined, text: string) => { if (n) parts.push(`${n} ${text}`); };
  add(c.excluded_unprojected, "excluded for a missing projection (not counted as zero)");
  add(c.excluded_position_limit, "excluded by a roster position limit");
  add(c.no_possible_gain, "with no possible gain over the window");
  add(c.not_evaluated_cap, "beyond the per-position evaluation cap (not evaluated)");
  return `Compared ${c.considered} eligible free ${c.considered === 1 ? "agent" : "agents"}${parts.length ? `: ${parts.join(" · ")}` : ""}`;
}

/**
 * The window the plan actually covers, from the weeks the backend evaluated: near the end of the season fewer later
 * weeks remain than the horizon asked for, and the gains are over those weeks only.
 */
export function stashWindowText(p: { horizon: number; weeks?: number[] | null; later_weeks?: number[] | null }): string {
  const later = p.later_weeks ?? (p.weeks ? p.weeks.slice(1) : null);
  if (!later) return `${p.horizon} later weeks asked for, plus the current week`;
  if (later.length === 0) return "no later week remains";
  const span = later.length === 1 ? `week ${later[0]}` : `weeks ${later[0]}–${later[later.length - 1]}`;
  const short = later.length < p.horizon ? `; only ${later.length} of the ${p.horizon} asked for remain` : "";
  return `${later.length} later ${later.length === 1 ? "week" : "weeks"} (${span}${short}), plus the current week`;
}
