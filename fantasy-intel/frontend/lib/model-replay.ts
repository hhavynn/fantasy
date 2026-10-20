// Types and pure helpers for the weekly model replay (GET /api/projections/replay). Every MAE and every difference
// comes from the backend (scripts/model_replay.py offline, app/analytics/model_replay.py on request); these helpers
// only pick the comparison to show and phrase it.

export const REPLAY_GROUPS = ["ALL", "QB", "RB", "WR", "TE"] as const;
export type ReplayGroup = (typeof REPLAY_GROUPS)[number];
export type ReplayMetric = "model" | "prior_version" | "recent_avg";
export type Direction = "better" | "worse" | "same";

export type ReplayCell = { n: number; mae: Record<ReplayMetric, number | null> };
export type ReplayWeek = { week: number; groups: Record<ReplayGroup, ReplayCell> };
export type ReplayModel = { version: string; fingerprint?: string; components?: Record<string, string[]> };
export type ReplaySnapshot = {
  schema_version: string; season: number; generated_at: string; model: ReplayModel; prior_version: ReplayModel;
  data: { fingerprint: string; last_week: number | null }; population: string;
  weeks: ReplayWeek[]; totals: Record<ReplayGroup, ReplayCell>; notes: string[];
};
export type DiffCell = { diff: number; direction: Direction; n_new: number; n_old: number } | null;
export type SeasonCell = { mae_new: number | null; mae_old: number | null; diff: number | null; direction: Direction | null; n_new: number; n_old: number };
export type ReplayComparison = {
  against?: { generated_at: string; model: ReplayModel };
  weeks: { week: number; groups: Record<ReplayGroup, DiffCell> }[];
  tally: Record<ReplayGroup, Record<Direction, number>>;
  season: { weeks: number[]; groups: Record<ReplayGroup, SeasonCell> };   // both sides over the same weeks
};
export type ReplayHistoryRow = { generated_at: string; model: ReplayModel; weeks: number[]; mae: number | null; n: number };
export type ReplayResponse = {
  schema_version: string; season: number; available: boolean; unavailable_reason: string | null; same_within: number;
  latest: ReplaySnapshot | null; previous: ReplaySnapshot | null;
  vs_previous: ReplayComparison | null; vs_prior: ReplayComparison | null; history: ReplayHistoryRow[];
};

export type ReplayView = {
  mode: "previous_snapshot" | "prior_version";
  label: string;          // what the deltas are measured against
  comparison: ReplayComparison;
};

/** The deltas shown: against the previous model's snapshot once a model change exists, otherwise against the
 *  previous production version replayed on the same player-weeks. */
export function replayView(d: ReplayResponse): ReplayView | null {
  if (!d.latest) return null;
  if (d.vs_previous && d.previous) {
    const fp = d.previous.model.fingerprint ? ` · ${d.previous.model.fingerprint.slice(0, 7)}` : "";
    return { mode: "previous_snapshot", comparison: d.vs_previous,
      label: `the previous model (${d.previous.model.version}${fp}, replayed ${d.previous.generated_at.slice(0, 10)})` };
  }
  if (d.vs_prior) return { mode: "prior_version", comparison: d.vs_prior, label: `${d.latest.prior_version.version}` };
  return null;
}

export function weeksWith(c: ReplayComparison, group: ReplayGroup, dir: Direction): number[] {
  return c.weeks.filter((w) => w.groups[group]?.direction === dir).map((w) => w.week);
}

const list = (weeks: number[]) => (weeks.length === 1 ? `week ${weeks[0]}` : `weeks ${weeks.slice(0, -1).join(", ")} and ${weeks[weeks.length - 1]}`);

/** "Closer in weeks 1 and 3, further in week 2, about the same in week 4." (all positions together) */
export function replaySummary(c: ReplayComparison, group: ReplayGroup = "ALL"): string {
  const parts: string[] = [];
  const better = weeksWith(c, group, "better");
  const worse = weeksWith(c, group, "worse");
  const same = weeksWith(c, group, "same");
  if (better.length) parts.push(`closer in ${list(better)}`);
  if (worse.length) parts.push(`further off in ${list(worse)}`);
  if (same.length) parts.push(`about the same in ${list(same)}`);
  if (!parts.length) return "No week to compare yet.";
  const s = parts.join(", ");
  return s[0].toUpperCase() + s.slice(1) + ".";
}

export function diffFor(c: ReplayComparison, week: number, group: ReplayGroup): DiffCell {
  return c.weeks.find((w) => w.week === week)?.groups[group] ?? null;
}

/** The season row's label: plain "Season" when the comparison covers every replayed week, otherwise the weeks
 *  both sides replayed (the row's MAE and change are then both computed over those weeks only). */
export function seasonLabel(c: ReplayComparison, replayedWeeks: number[]): string {
  const shared = c.season.weeks;
  if (shared.length === replayedWeeks.length) return "Season";
  if (!shared.length) return "Season (no shared week)";
  return `Weeks ${shared[0]}–${shared[shared.length - 1]} (both replays)`;
}
