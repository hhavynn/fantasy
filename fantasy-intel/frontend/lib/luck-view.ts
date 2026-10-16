// Pure view helpers for the Luck lab: arrange backend numbers for the shared chart primitives.
// Nothing here computes a new statistic.
import type { BarDatum, ScatterPoint } from "@/components/charts";
import type { LlLeader, LlTimeline } from "@/lib/luck-types";

/** "wk 6", "wk 6–7", or "period 6" when the backend has no week list. */
export function periodLabel(weeks: number[] | null | undefined, period: number): string {
  return weeks && weeks.length ? (weeks.length > 1 ? `wk ${weeks[0]}–${weeks[weeks.length - 1]}` : `wk ${weeks[0]}`) : `period ${period}`;
}

/** Axis label for a period: "6", "6–7" or "P6". */
export function shortPeriod(weeks: number[] | null | undefined, period: number): string {
  return weeks && weeks.length ? (weeks.length > 1 ? `${weeks[0]}–${weeks[weeks.length - 1]}` : `${weeks[0]}`) : `P${period}`;
}

/** Signed number text with a real minus sign ("+0.80", "−1.2"). `null` gives "—". */
export function signedText(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const s = Math.abs(v).toFixed(digits);
  return v > 0 && Number(s) !== 0 ? `+${s}` : v < 0 && Number(s) !== 0 ? `−${s}` : s;
}

/** One diverging bar per team, most schedule luck first. */
export function luckBars(rows: readonly LlLeader[]): BarDatum[] {
  return [...rows]
    .sort((a, b) => b.schedule_luck - a.schedule_luck)
    .map((r) => ({ label: r.is_mine ? `${r.name} (default)` : r.name, value: r.schedule_luck }));
}

/**
 * Actual win equivalents against expected wins, one point per team. Labelled: your team and any
 * team at least `minLuck` wins off the diagonal (the rest keep a tooltip).
 */
export function scatterPoints(rows: readonly LlLeader[], minLuck = 1): ScatterPoint[] {
  return rows.map((p) => ({
    id: p.team_id,
    x: p.expected_wins,
    y: p.actual.win_equivalents,
    label: p.is_mine || Math.abs(p.schedule_luck) >= minLuck ? p.name : undefined,
    highlight: p.is_mine,
    title: `${p.name}: actual ${p.actual.win_equivalents.toFixed(1)}, expected ${p.expected_wins.toFixed(2)}, luck ${signedText(p.schedule_luck)}`,
  }));
}

/** Period-by-period series for the week-by-week charts, in the order the backend returned them. */
export function timelineSeries(t: readonly LlTimeline[]) {
  return {
    labels: t.map((x) => shortPeriod(x.weeks, x.matchup_period)),
    score: t.map((x) => x.score),
    opponent: t.map((x) => x.opponent_score),
    luck: t.map((x) => x.luck),
    cumulative: t.map((x) => x.cumulative_luck),
  };
}

/**
 * Heat for a matrix cell, 0..1: the expected wins (share of the league that period's score beat)
 * mapped onto a visible-but-quiet range so the lightest cells stay readable.
 */
export function heatOf(expectedWins: number): number {
  return Math.min(1, Math.max(0, 0.08 + 0.5 * expectedWins));
}
