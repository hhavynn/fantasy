// Bye and lineup-coverage planner (bye-planner.v1, backend/app/analytics/bye_planner.py): types and pure helpers that
// only arrange what the backend computed (states, counts, statuses). Nothing here decides whether a week is covered.
// An unknown player is never shown as able or as unable; a week's status is the backend's.

import { withLeague } from "./league-scope.ts";
import { plural } from "./plural.ts";
import type { ProvenancePayload } from "./signal-provenance.ts";

export type ByeWeekStatus = "covered" | "short" | "unconfirmed";
export type PlayerWeekState = "available" | "bye" | "out" | "unknown";
export type UnknownCode =
  | "no_team" | "schedule_missing" | "team_schedule_incomplete" | "availability_unknown" | "return_unconfirmed"
  | "injury_designation";

export type PlayerWeek = {
  state: PlayerWeekState;
  code: string;
  source: "schedule" | "availability" | "unknown";
  detail: string | null;
  opponent: string | null;
  home: boolean | null;
  kickoff: string | null;
};

export type PlannerPlayer = {
  espn_player_id: number;
  name: string | null;
  position: string | null;
  pro_team: string | null;
  injury_status: string | null;
  slot: "starter" | "bench" | "ir" | "taxi";
  /** Counts toward coverage: active (not IR or taxi) and eligible for a starting slot. */
  counts: boolean;
  eligible_slots: string[];
  bye_week: number | null;
  availability: { status: string | null; earliest_eligible_week: number | null; eligibility_basis: string | null; nfl_designation_label: string | null } | null;
  weeks: Record<string, PlayerWeek>;
};

export type PlannerSlot = {
  slot: string;
  eligible_positions: string[];
  required: number;
  covered: number;
  unknown: number;
  uncovered: number;
  /** Eligible rostered players per state this week (a FLEX counts every eligible player, also those in fixed slots). */
  available: number;
  uncertain: number;
  bye: number;
  out: number;
};

export type PlannerWeek = {
  week: number;
  phase: "regular" | "playoffs" | null;
  matchup_period: number | null;
  status: ByeWeekStatus;
  status_text: string;
  conflict: boolean;
  required: number;
  covered: number;
  unknown: number;
  uncovered: number;
  unresolved: number;
  slots: PlannerSlot[];
  short_slots: string[];
  unconfirmed_slots: string[];
  byes: number[];
  bye_cluster: boolean;
  starters_on_bye: number[];
  out: number[];
  uncertain: { espn_player_id: number; code: UnknownCode | string }[];
  bench_alternatives: { espn_player_id: number; slots: string[] }[];
};

export type ByePlanner = {
  contract: string;
  league: { league_id: number; season: number; name: string; current_week: number; final_scoring_period: number; last_regular_season_week: number | null };
  team: { team_id: number; name: string; abbrev: string | null; is_mine: boolean };
  teams: { team_id: number; name: string; abbrev: string | null; is_mine: boolean }[];
  query: { team_id: number };
  window: {
    espn_current_week: number; first_actionable_week: number | null; weeks: number[]; remaining_weeks: boolean;
    uncertain: boolean | null; warning: string | null; reason: string | null; regular_season_through: number | null;
    playoff_weeks: number[];
  };
  slots: { starting: { slot: string; count: number; eligible_positions: string[] }[]; unresolved: { slot: string; count: number; reason: string }[]; known: boolean };
  schedule: { available: boolean; weeks_loaded: number[]; weeks_missing: number[]; incomplete_teams: string[] };
  players: PlannerPlayer[];
  weeks: PlannerWeek[];
  summary: {
    weeks: number; covered: number[]; short: number[]; unconfirmed: number[]; bye_cluster_weeks: number[];
    conflict_weeks: number[]; first_conflict_week: number | null; most_byes: { week: number; count: number } | null;
  };
  rules: { bye_cluster_min: number; sources: Record<string, string>; unknown_reasons: Record<string, string>; statuses: Record<ByeWeekStatus, string> };
  notes: string[];
  provenance?: ProvenancePayload;
};

/** A status is never colour alone: each carries a glyph and a word. */
export const STATUS_GLYPH: Record<ByeWeekStatus, string> = { covered: "✓", short: "✕", unconfirmed: "?" };
export const STATUS_WORD: Record<ByeWeekStatus, string> = { covered: "Covered", short: "Short", unconfirmed: "Not confirmed" };

/** One slot's status in one week, from the backend's counts: any uncovered seat is short, else any unknown seat. */
export function slotStatus(s: Pick<PlannerSlot, "unknown" | "uncovered">): ByeWeekStatus {
  if (s.uncovered > 0) return "short";
  if (s.unknown > 0) return "unconfirmed";
  return "covered";
}

const POSITION_FILTERS = new Set(["QB", "RB", "WR", "TE"]);

/** The Waivers view for the first slot that needs help (short first, then unconfirmed); a flex slot has no filter. */
export function waiversPath(w: Pick<PlannerWeek, "short_slots" | "unconfirmed_slots">): string {
  const slot = w.short_slots[0] ?? w.unconfirmed_slots[0];
  if (slot === "K" || slot === "D/ST") return `/waivers?view=STREAMING&pos=${encodeURIComponent(slot)}`;
  if (slot && POSITION_FILTERS.has(slot)) return `/waivers?pos=${slot}`;
  return "/waivers";
}

function withParams(path: string, params: Record<string, string>): string {
  const [base, query = ""] = path.split("?");
  const qs = new URLSearchParams(query);
  for (const [k, v] of Object.entries(params)) qs.set(k, v);
  return `${base}?${qs}`;
}

/** Links for one planned week, in the exact league (`/l/<lg>` when scoped), team and week. */
export function weekLinks(w: Pick<PlannerWeek, "week" | "short_slots" | "unconfirmed_slots">, teamId: number, league: string | null | undefined) {
  const ctx = { week: String(w.week), team: String(teamId) };
  return {
    startSit: withLeague(withParams("/start-sit", ctx), league),
    waivers: withLeague(withParams(waiversPath(w), ctx), league),
  };
}

export type NameOf = (id: number) => string;

export function playerNames(players: readonly PlannerPlayer[]): NameOf {
  const byId = new Map(players.map((p) => [p.espn_player_id, p.name ?? `Player #${p.espn_player_id}`]));
  return (id) => byId.get(id) ?? `Player #${id}`;
}

const list = (names: string[]) => (names.length <= 2 ? names.join(" and ") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`);

/** The week's headline sentence: the status first, then the bye count. */
export function weekHeadline(w: PlannerWeek): string {
  const byes = w.byes.length ? ` ${w.byes.length} ${plural(w.byes.length, "player")} on bye.` : "";
  if (w.status === "short") {
    return `Week ${w.week}: ${w.uncovered} starting ${plural(w.uncovered, "slot")} cannot be filled from the roster (${w.short_slots.join(", ")}).${byes}`;
  }
  if (w.status === "unconfirmed") {
    const parts = [];
    if (w.unknown) parts.push(`${w.unknown} ${plural(w.unknown, "slot")} ${plural(w.unknown, "depends", "depend")} on players whose availability is unknown`);
    if (w.unresolved) parts.push(`${w.unresolved} ${plural(w.unresolved, "slot is", "slots are")} not evaluated`);
    return `Week ${w.week}: not confirmed. ${parts.length ? `${parts.join("; ")}.` : "Roster slots are not known."}${byes}`;
  }
  return `Week ${w.week}: every starting slot is covered.${byes}`;
}

export type ReasonLine = { kind: "bye" | "out" | "unknown" | "alternatives"; label: string; text: string };

/** The evidence behind a week, by kind: exact byes, verified absences, unknowns, then bench alternatives. */
export function weekReasons(w: PlannerWeek, players: readonly PlannerPlayer[], unknownText: Record<string, string> = {}): ReasonLine[] {
  const name = playerNames(players);
  const byId = new Map(players.map((p) => [p.espn_player_id, p]));
  const tag = (id: number) => {
    const p = byId.get(id);
    return `${name(id)}${p?.position ? ` (${p.position}${p.pro_team ? `, ${p.pro_team}` : ""})` : ""}`;
  };
  const out: ReasonLine[] = [];
  if (w.byes.length) {
    const starters = new Set(w.starters_on_bye);
    out.push({ kind: "bye", label: w.bye_cluster ? "Bye cluster" : "On bye",
      text: list(w.byes.map((id) => `${tag(id)}${starters.has(id) ? ", current starter" : ""}`)) });
  }
  if (w.out.length) {
    out.push({ kind: "out", label: "Out (NFL reserve list)",
      text: list(w.out.map((id) => { const d = byId.get(id)?.weeks[String(w.week)]?.detail; return d ? `${tag(id)}: ${d}` : tag(id); })) });
  }
  if (w.uncertain.length) {
    out.push({ kind: "unknown", label: "Unknown",
      text: list(w.uncertain.map((u) => `${tag(u.espn_player_id)}: ${byId.get(u.espn_player_id)?.weeks[String(w.week)]?.detail ?? unknownText[u.code] ?? "availability not established"}`)) });
  }
  if (w.bench_alternatives.length) {
    out.push({ kind: "alternatives", label: "Bench alternatives",
      text: list(w.bench_alternatives.map((b) => `${name(b.espn_player_id)} (${b.slots.join(", ")})`)) });
  }
  return out;
}

/** "3 covered, 1 short, 2 not confirmed" over the planned weeks (counts the backend's statuses). */
export function summaryText(s: ByePlanner["summary"]): string {
  const parts = [`${s.covered.length} covered`, `${s.short.length} short`, `${s.unconfirmed.length} not confirmed`];
  return `${s.weeks} ${plural(s.weeks, "week")}: ${parts.join(", ")}`;
}
