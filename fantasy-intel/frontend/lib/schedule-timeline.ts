// Pure helpers for the schedule-intelligence timeline. They only arrange backend fields (labels,
// ranks, opponents); nothing here computes a new number.
import type { SiLabel, SiMetric, SiPlayer, SiWeek } from "@/lib/types";

export type TimelineKind = "game" | "bye" | "unknown" | "none";

export type TimelineCell = {
  week: number;
  phase: "regular" | "playoffs" | null;
  kind: TimelineKind;
  label: SiLabel;
  /** "vs" (home), "@" (away) or null when there is no game. */
  where: "vs" | "@" | null;
  opponent: string | null;
  /** Rank of the opponent on the position's primary metric (1 = most permissive), if evidenced. */
  rank: number | null;
  rankedDefenses: number | null;
  /** The matchup is still ahead (the backend marks earlier weeks as not actionable). */
  actionable: boolean;
  /** Plain-language description for tooltips and assistive tech. */
  description: string;
};

export type TimelineColumn = {
  week: number;
  phase: "regular" | "playoffs";
  /** The first fantasy-playoff column: drawn with a divider. */
  startsPlayoffs: boolean;
  matchupPeriod: number | null;
};

/** Glyphs that carry the label next to the colour: a label is never colour alone. */
export const LABEL_GLYPH: Record<SiLabel, string> = {
  permissive: "▲", middle: "●", tougher: "▼", insufficient: "?", unavailable: "?", bye: "",
};

export function primaryMetric(w: SiWeek): SiMetric | undefined {
  return w.evidence?.metrics.find((m) => m.metric === w.evidence?.primary_metric);
}

export function describeWeek(w: SiWeek): string {
  const where = w.status === "scheduled" ? `${w.home ? "home vs" : "away at"} ${w.opponent}` : w.status === "bye" ? "bye" : "no opponent";
  const p = primaryMetric(w);
  const rank = p?.rank ? `, opponent rank ${p.rank} of ${p.ranked_defenses}` : "";
  return `Week ${w.week}: ${where}, ${w.label_text.toLowerCase()}${rank}`;
}

export function toCell(w: SiWeek): TimelineCell {
  const p = primaryMetric(w);
  const kind: TimelineKind = w.status === "scheduled" ? "game" : w.status === "bye" ? "bye" : "unknown";
  return {
    week: w.week,
    phase: w.phase,
    kind,
    label: w.label,
    where: kind === "game" ? (w.home ? "vs" : "@") : null,
    opponent: kind === "game" ? (w.opponent ?? null) : null,
    rank: p?.rank ?? null,
    rankedDefenses: p?.rank ? p.ranked_defenses : null,
    actionable: w.actionable,
    description: describeWeek(w),
  };
}

/** One cell per column week for a player; a week the backend returned no row for is "none". */
export function buildTimeline(columns: readonly number[], player: SiPlayer): TimelineCell[] {
  const byWeek = new Map(player.weeks.map((w) => [w.week, w]));
  return columns.map((week) => {
    const w = byWeek.get(week);
    if (!w) {
      return { week, phase: null, kind: "none", label: "unavailable", where: null, opponent: null, rank: null, rankedDefenses: null, actionable: false, description: `Week ${week}: no schedule row` };
    }
    return toCell(w);
  });
}

/** Column headers: the weeks in the window, each tagged regular season or fantasy playoffs. */
export function buildColumns(weeks: readonly number[], playoffWeeks: readonly number[], matchupPeriods: Record<string, number | null>): TimelineColumn[] {
  const playoffs = new Set(playoffWeeks);
  let seenPlayoffs = false;
  return weeks.map((week) => {
    const isPlayoff = playoffs.has(week);
    const startsPlayoffs = isPlayoff && !seenPlayoffs;
    if (isPlayoff) seenPlayoffs = true;
    return { week, phase: isPlayoff ? "playoffs" : "regular", startsPlayoffs, matchupPeriod: matchupPeriods[String(week)] ?? null };
  });
}

/** The player's first actionable week (the matchup that is still ahead). */
export function nextWeekOf(player: SiPlayer): SiWeek | undefined {
  return player.weeks.find((w) => w.actionable);
}

/** Players per label of their next actionable week (a sum of backend labels, not a new analytic). */
export function countNextByLabel(players: readonly SiPlayer[]): Record<SiLabel, number> {
  const out: Record<SiLabel, number> = { permissive: 0, middle: 0, tougher: 0, insufficient: 0, unavailable: 0, bye: 0 };
  for (const p of players) {
    const next = nextWeekOf(p);
    if (next) out[next.label] += 1;
  }
  return out;
}
