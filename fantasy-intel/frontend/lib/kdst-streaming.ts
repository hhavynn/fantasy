// K and D/ST streaming planner (kdst-streaming.v1, backend/app/analytics/kdst_streaming.py): types and pure helpers that
// only arrange what the backend computed. Nothing here picks a player, adds points or fills a missing projection: a week
// the backend did not plan has no player and no number.

import { withLeague } from "./league-scope.ts";
import { plural } from "./plural.ts";
import type { ProvenancePayload } from "./signal-provenance.ts";

export type StreamPosition = "K" | "D/ST";
export type Eligibility = "free_agent" | "waiver_claim" | "hypothetical" | "unconfirmed" | "held";
export type CellState = "plays" | "bye" | "out" | "unknown" | "empty";

export type StreamPerson = {
  espn_player_id: number;
  name: string | null;
  pro_team: string | null;
  injury_status: string | null;
  provider_status: string | null;
};

export type ModelCell = {
  expected_points: number | null;
  coverage_status: string | null;
  implied_total: number | null;
  model_version: string;
  forecast_horizon: number;
};

export type StreamStart = StreamPerson & {
  points: number | null;
  state: CellState;
  detail: string | null;
  opponent: string | null;
  home: boolean | null;
  kickoff: string | null;
  eligibility: Eligibility;
  conflicts: { espn_player_id: number; name: string | null; position: string | null }[];
  model: ModelCell | null;
  action?: "add" | "keep";
  delta?: number | null;
};

export type StreamWeek = {
  week: number;
  horizon: number;
  phase: "regular" | "playoffs" | null;
  outlook: boolean;
  schedule_loaded: boolean;
  candidates_with_game: number;
  projected_candidates: number;
  planned: boolean;
  reason: string | null;
  baseline: { points: number; espn_player_id: number; name: string | null; pro_team: string | null; rank: number; available_projected: number } | null;
  /** Null player, points and state when the week is not planned (no player and no number). */
  hold: { player: StreamPerson | null; points: number | null; state: CellState | null; detail: string | null; opponent: string | null; home: boolean | null; kickoff: string | null };
  action: "hold" | "add" | "keep" | null;
  start: StreamStart | null;
  points?: number | null;
  alternatives: StreamStart[];
  gap: { code: "empty_slot" | "below_baseline"; text: string } | null;
  reasons: string[];
};

export type StreamPositionPlan = {
  position: StreamPosition;
  slots: number;
  status: "planned" | "no_plan" | "no_weeks" | "not_started";
  incumbents: StreamPerson[];
  weeks: StreamWeek[];
  planned_weeks: number[];
  capped_weeks: number[];
  plan: {
    moves: number; move_cost: number; cost_total: number; projected_points: number; net_points: number;
    hold_points: number | null; gain_vs_hold: number | null; hold_unknown_weeks: number[];
  } | null;
  no_plan_reason: string | null;
  this_week: { week: number; best: StreamStart | null; best_confirmed: StreamStart | null; hold_points: number | null } | null;
  pool: { free_agents: number; candidates: number; cap: number; unprojected: number; excluded: number } | null;
  excluded: (StreamPerson & { reason: string })[];
  model: Record<string, { run: { model_version: string; forecast_horizon: number; artifact_verified: boolean } | null; reason: string | null }>;
};

export type KdstStreaming = {
  contract: string;
  league: { league_id: number; season: number; name: string; current_week: number; final_scoring_period: number; last_regular_season_week: number | null };
  team: { team_id: number; name: string; abbrev: string | null; is_mine: boolean };
  teams: { team_id: number; name: string; abbrev: string | null; is_mine: boolean }[];
  query: { team_id: number; weeks: number; max_moves: number | null; move_cost: number };
  provider: string;
  projection_source: string;
  window: {
    espn_current_week: number; first_actionable_week: number | null; weeks: number[]; remaining_weeks: boolean;
    uncertain: boolean | null; warning: string | null; reason: string | null;
  };
  schedule: { available: boolean; weeks_missing: number[] };
  positions: StreamPositionPlan[];
  rules: {
    min_projected: number; min_coverage: number; outlook_from: number; max_pool: number; stream_rank: number;
    weeks: [number, number]; max_moves_limit: number; max_move_cost: number; eligibility: Record<Eligibility, string>;
  };
  notes: string[];
  provenance?: ProvenancePayload;
};

export const POSITION_NAME: Record<StreamPosition, string> = { K: "Kicker", "D/ST": "D/ST" };
const NOUN: Record<StreamPosition, string> = { K: "kicker", "D/ST": "D/ST" };
const PAGE: Record<StreamPosition, string> = { K: "/kickers", "D/ST": "/defenses" };

/** Short eligibility words; the backend's sentence is the title. Hypothetical is never shown as confirmed. */
export const ELIGIBILITY_WORD: Record<Eligibility, string> = {
  free_agent: "Free agent", waiver_claim: "Waiver claim", hypothetical: "If still available",
  unconfirmed: "Not confirmed", held: "Held",
};
export const ELIGIBILITY_TONE: Record<Eligibility, string> = {
  free_agent: "good", waiver_claim: "warn", hypothetical: "info", unconfirmed: "warn", held: "",
};

/** The existing kicker or defense page for a week, in the exact league (`/l/<lg>` when scoped). */
export function positionPageLink(position: StreamPosition, week: number, league: string | null | undefined): string {
  return withLeague(`${PAGE[position]}?week=${week}`, league);
}

/** A stable DOM id for one position-week ("stream-dst-week-9"). */
export function weekAnchor(position: StreamPosition, week: number): string {
  return `stream-${position === "K" ? "k" : "dst"}-week-${week}`;
}

const pts = (v: number | null | undefined) => (v === null || v === undefined ? null : v.toFixed(1));

/** The compact label of one week: what to do and with whom. A week the backend did not plan says so, with no number. */
export function weekAction(w: StreamWeek, position: StreamPosition): string {
  if (!w.planned || w.action === null) return "Not planned";
  if (w.action === "hold") {
    const who = w.hold.player?.name;
    return who ? `Keep ${who}` : `No ${NOUN[position]} rostered`;
  }
  const who = w.start?.name ?? `Player #${w.start?.espn_player_id ?? "?"}`;
  return w.action === "add" ? `Add ${who}` : `Keep ${who}`;
}

/** The projected points of the planned start, or null (shown as a dash) when the week is not planned. */
export function weekPoints(w: StreamWeek): number | null {
  if (!w.planned || w.action === null) return null;
  return w.points ?? null;
}

/** This week's advice in one sentence, from the backend's plan and best streamer. */
export function currentAdvice(p: StreamPositionPlan): string {
  if (p.status === "not_started") return p.no_plan_reason ?? `The league does not start a ${NOUN[p.position]}.`;
  const first = p.weeks[0];
  if (!first) return "No remaining week to plan.";
  if (!first.planned) return `Week ${first.week}: no plan. ${first.reason ?? ""}`.trim();
  if (p.plan === null) return `Week ${first.week}: no plan. ${p.no_plan_reason ?? ""}`.trim();
  const label = weekAction(first, p.position);
  const v = pts(weekPoints(first));
  const best = p.this_week?.best;
  const bestDiffers = best && first.start?.espn_player_id !== best.espn_player_id && first.action !== null;
  const tail = bestDiffers ? ` Best for this week alone: ${best.name ?? `Player #${best.espn_player_id}`} (${pts(best.points) ?? "—"}).` : "";
  return `Week ${first.week}: ${label}${v !== null ? ` (${v} projected)` : ""}.${tail}`;
}

/** "3 weeks · 2 add/drops · 31.0 projected · +21.0 vs keeping" (only the backend's numbers; unknown stays unknown). */
export function planSummary(p: StreamPositionPlan): string {
  const n = p.planned_weeks.length;
  if (!p.plan) return n ? `${n} ${plural(n, "week")} · no feasible plan` : "No week planned";
  const parts = [`${n} ${plural(n, "week")}`, `${p.plan.moves} ${plural(p.plan.moves, "add/drop")}`,
    `${p.plan.projected_points.toFixed(1)} projected`];
  if (p.plan.cost_total > 0) parts.push(`${p.plan.net_points.toFixed(1)} after costs`);
  parts.push(p.plan.gain_vs_hold === null ? "keeping cannot be compared"
    : `${p.plan.gain_vs_hold >= 0 ? "+" : "−"}${Math.abs(p.plan.gain_vs_hold).toFixed(1)} vs keeping`);
  return parts.join(" · ");
}

type SP = Record<string, string | string[] | undefined>;
const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

/** The planner's own URL params (stream_weeks, max_moves, move_cost), validated; invalid values fall back to the
 *  backend's defaults instead of producing a 422. */
export function streamParams(sp: SP): { weeks?: number; maxMoves?: number; moveCost?: number } {
  const out: { weeks?: number; maxMoves?: number; moveCost?: number } = {};
  const w = Number(one(sp.stream_weeks));
  if (Number.isInteger(w) && w >= 2 && w <= 4) out.weeks = w;
  const raw = one(sp.max_moves);
  const m = raw === undefined || raw === "" ? NaN : Number(raw);
  if (Number.isInteger(m) && m >= 0 && m <= 4) out.maxMoves = m;
  const c = Number(one(sp.move_cost));
  if (one(sp.move_cost) !== undefined && one(sp.move_cost) !== "" && Number.isFinite(c) && c >= 0 && c <= 10) out.moveCost = c;
  return out;
}

/** The backend path for the planner, for the team and the params above. */
export function streamingPath(team: string | undefined, sp: SP): string {
  const q = new URLSearchParams();
  if (team) q.set("team_id", team);
  const p = streamParams(sp);
  if (p.weeks !== undefined) q.set("weeks", String(p.weeks));
  if (p.maxMoves !== undefined) q.set("max_moves", String(p.maxMoves));
  if (p.moveCost !== undefined) q.set("move_cost", String(p.moveCost));
  const qs = q.toString();
  return `/api/schedule-intelligence/kdst-streaming${qs ? `?${qs}` : ""}`;
}

/** "vs NYJ" / "@ NYJ", or null when the opponent is unknown. */
export function opponentText(x: { opponent: string | null; home: boolean | null }): string | null {
  if (!x.opponent) return null;
  return `${x.home ? "vs" : "@"} ${x.opponent}`;
}
