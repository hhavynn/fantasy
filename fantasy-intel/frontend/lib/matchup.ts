// Pure presentation helpers for the matchup page and the Overview matchup card. They select, order and
// compare numbers the backend already computed; nothing here is a new analytic.
import type { Matchup, MatchupStatus, TeamSide } from "./matchup-types.ts";

export type Basis = "actual" | "projected" | "known";

/** The one number shown big for a team, and what kind of number it is. */
export type Headline = { value: number | null; basis: Basis | null; label: string; incomplete: boolean };

const round2 = (x: number) => Math.round(x * 100) / 100;

/**
 * Started matchups lead with actual points; otherwise the complete projection. When a projection is
 * incomplete only the known-only total exists: it is labelled as such and never treated as complete.
 */
export function headline(side: TeamSide, status: MatchupStatus): Headline {
  if ((status === "final" || status === "in_progress") && side.actual_points !== null) {
    return { value: side.actual_points, basis: "actual", label: status === "final" ? "Final" : "Actual so far", incomplete: false };
  }
  if (side.projected_points !== null) {
    return { value: side.projected_points, basis: "projected", label: "Projected", incomplete: false };
  }
  if (side.known_points !== null) {
    return { value: side.known_points, basis: "known", label: "Known only", incomplete: true };
  }
  return { value: null, basis: null, label: "No projection", incomplete: false };
}

export type Margin = { value: number | null; basis: "actual" | "projected" | null; reason: string | null };

/** My total minus the opponent's, only when both sides are the same complete kind of number. */
export function margin(me: Headline, opp: Headline): Margin {
  if (me.value === null || opp.value === null || me.basis === null || opp.basis === null) {
    return { value: null, basis: null, reason: "A margin needs a total for both teams." };
  }
  if (me.basis === "known" || opp.basis === "known") {
    return { value: null, basis: null, reason: "A projection is incomplete, so a margin would not be a fair comparison." };
  }
  if (me.basis !== opp.basis) return { value: null, basis: null, reason: "The two totals are different kinds of number." };
  return { value: round2(me.value - opp.value), basis: me.basis, reason: null };
}

/** My share (0-100) of the two totals combined, for the two-tone bar. Not a win probability. */
export function share(me: Headline, opp: Headline): number | null {
  if (me.value === null || opp.value === null || me.basis === null || me.basis !== opp.basis || me.basis === "known") return null;
  if (me.value < 0 || opp.value < 0 || me.value + opp.value <= 0) return null;
  return (me.value / (me.value + opp.value)) * 100;
}

const SLOT_ORDER = ["QB", "RB", "WR", "TE", "RB/WR", "WR/TE", "FLEX", "OP", "D/ST", "K"];
const slotRank = (slot: string) => {
  const i = SLOT_ORDER.indexOf(slot);
  return i === -1 ? SLOT_ORDER.length : i;
};

export type SlotRow<T> = { slot: string; left: T | null; right: T | null };

/** Line two lineups up slot by slot (the first RB against the first RB, FLEX against FLEX). */
export function pairSlots<T extends { slot: string }>(left: readonly T[], right: readonly T[]): SlotRow<T>[] {
  const slots = [...new Set([...left, ...right].map((p) => p.slot))]
    .sort((a, b) => slotRank(a) - slotRank(b) || a.localeCompare(b));
  const rows: SlotRow<T>[] = [];
  for (const slot of slots) {
    const l = left.filter((p) => p.slot === slot);
    const r = right.filter((p) => p.slot === slot);
    for (let i = 0; i < Math.max(l.length, r.length); i++) rows.push({ slot, left: l[i] ?? null, right: r[i] ?? null });
  }
  return rows;
}

/** Sum of a lineup's numbers, or null when any is missing: a partial sum is not a lineup total. */
export function sumAll(values: readonly (number | null)[]): number | null {
  if (values.length === 0 || values.some((v) => v === null)) return null;
  return round2((values as number[]).reduce((a, b) => a + b, 0));
}

/** Which side has the higher number in a row (null on a tie or when either is unknown). */
export function leader(left: number | null, right: number | null): "left" | "right" | null {
  if (left === null || right === null || left === right) return null;
  return left > right ? "left" : "right";
}

const HEALTHY = new Set(["", "ACTIVE", "NORMAL"]);
/** True when ESPN carries a designation other than active/normal (Q, D, O, IR, suspended…). */
export function hasDesignation(status: string | null | undefined): boolean {
  return !!status && !HEALTHY.has(status.toUpperCase());
}

export type WeekParam = { kind: "none" } | { kind: "ok"; week: number } | { kind: "invalid"; raw: string };

/** Reads `?week=`: absent, a positive integer, or invalid (anything else, so the backend never gets garbage). */
export function parseWeekParam(raw: string | string[] | undefined): WeekParam {
  const v = Array.isArray(raw) ? raw[0] : raw;
  if (v === undefined || v === "") return { kind: "none" };
  return /^[1-9]\d{0,2}$/.test(v) ? { kind: "ok", week: Number(v) } : { kind: "invalid", raw: v };
}

/** The weeks a week switcher offers: the weeks with a matchup plus the one being viewed. */
export function switcherWeeks(available: readonly number[], viewing: number): number[] {
  return [...new Set([...available, viewing])].sort((a, b) => a - b);
}

export function statusLabel(status: MatchupStatus): string {
  switch (status) {
    case "upcoming": return "Not started";
    case "in_progress": return "In progress";
    case "final": return "Final";
    default: return "Status unknown";
  }
}

/** 1 -> "1st", 11 -> "11th", 22 -> "22nd". */
export function ordinal(n: number): string {
  const v = n % 100;
  return `${n}${v >= 11 && v <= 13 ? "th" : ["th", "st", "nd", "rd"][n % 10] ?? "th"}`;
}

export function recordText(r: { wins: number; losses: number; ties: number }): string {
  return `${r.wins}–${r.losses}${r.ties ? `–${r.ties}` : ""}`;
}

/** Query string for /matchup links, keeping only the parameters that are set. */
export function matchupHref(params: { week?: number | null; team_id?: string | null; lineup?: string | null }): string {
  const qs = new URLSearchParams();
  if (params.week) qs.set("week", String(params.week));
  if (params.team_id) qs.set("team_id", params.team_id);
  if (params.lineup) qs.set("lineup", params.lineup);
  const s = qs.toString();
  return s ? `/matchup?${s}` : "/matchup";
}

// ------------------------------------------------------------------ which lineup is shown --

/** "projected": each team's best lineup from ESPN's projections. "set": the lineup held in ESPN. */
export type LineupView = "projected" | "set";

export type PlayerRow = {
  slot: string;
  id: number | null;
  name: string | null;
  position: string | null;
  pro_team: string | null;
  injury: string | null;
  bye: boolean | null;
  /** Projected points, or actual points for a set lineup once the matchup has started. null = unknown. */
  value: number | null;
};

export const hasStarted = (status: MatchupStatus) => status === "in_progress" || status === "final";

/** Views with data for at least one side. */
export function availableViews(m: Pick<Matchup, "me" | "opponent">): LineupView[] {
  const sides = [m.me, m.opponent].filter((s): s is TeamSide => s !== null);
  const out: LineupView[] = [];
  if (sides.some((s) => s.lineup.length > 0)) out.push("projected");
  if (sides.some((s) => s.actual_lineup !== null)) out.push("set");
  return out;
}

/** The requested view if it has data; otherwise set once play has started, else projected, else whatever exists. */
export function chooseView(requested: string | string[] | undefined, m: Pick<Matchup, "me" | "opponent" | "status">): LineupView | null {
  const views = availableViews(m);
  const want = Array.isArray(requested) ? requested[0] : requested;
  if (want === "projected" || want === "set") {
    if (views.includes(want)) return want;
  }
  const preferred: LineupView = hasStarted(m.status) ? "set" : "projected";
  return views.includes(preferred) ? preferred : views[0] ?? null;
}

/** Starting lineup rows for one side in a view; null when this side has no data in that view. */
export function lineupRows(side: TeamSide, view: LineupView, status: MatchupStatus): PlayerRow[] | null {
  if (view === "projected") {
    return side.lineup.length === 0 ? null : side.lineup.map((s) => ({
      slot: s.slot, id: s.player_id, name: s.name, position: s.position, pro_team: s.pro_team,
      injury: s.injury_status, bye: s.on_bye, value: s.points,
    }));
  }
  return side.actual_lineup === null ? null : side.actual_lineup.map((p) => ({
    slot: p.slot, id: p.player_id, name: p.name, position: p.position, pro_team: p.pro_team,
    injury: p.injury_status, bye: p.on_bye, value: hasStarted(status) ? p.points : p.projected_points,
  }));
}

/** Bench rows for one side in a view (the set bench includes IR); null when there is no data. */
export function benchRows(side: TeamSide, view: LineupView, status: MatchupStatus): PlayerRow[] | null {
  if (view === "projected") {
    if (side.lineup.length === 0) return null;
    return side.bench.map((b) => ({
      slot: "BE", id: b.player_id, name: b.name, position: b.position, pro_team: b.pro_team,
      injury: b.injury_status, bye: b.on_bye, value: b.points,
    }));
  }
  return side.actual_bench === null ? null : side.actual_bench.map((p) => ({
    slot: p.slot, id: p.player_id, name: p.name, position: p.position, pro_team: p.pro_team,
    injury: p.injury_status, bye: p.on_bye, value: hasStarted(status) ? p.points : p.projected_points,
  }));
}

export type LineupTotal = { value: number | null; incomplete: boolean };

/**
 * Footer total for a side's lineup. Projected view uses the backend's own total (known-only when the
 * projection is incomplete); the set view sums the shown numbers and gives none if any is unknown.
 */
export function lineupTotal(side: TeamSide, view: LineupView, rows: readonly PlayerRow[] | null): LineupTotal {
  if (rows === null) return { value: null, incomplete: false };
  if (view === "projected") {
    return side.projected_points !== null
      ? { value: side.projected_points, incomplete: false }
      : { value: side.known_points, incomplete: side.known_points !== null };
  }
  const total = sumAll(rows.map((r) => r.value));
  return { value: total, incomplete: total === null };
}
