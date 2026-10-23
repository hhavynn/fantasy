// League analyzer (GET /api/league-analyzer, contract league-analyzer/v1; issue #191): types and pure view helpers.
// No Next, no fetch, so node --test covers them. The backend is authoritative: nothing here re-scores a team, and an
// unknown cell stays unknown (it sorts after every ranked team, never as the worst one).

export type LaStatus = "ranked" | "unknown" | "unverified";

export type LaCell = {
  status: LaStatus;
  points: number | null;              // projected points per week from the group's starting slots (evidence)
  per_slot: number | null;
  above_replacement: number | null;   // per slot, above the best available free agent
  vs_median: number | null;
  score: number | null;               // league-relative 0-100 within the group
  rank: number | null;
  of: number;
  reason: string | null;
};

export type LaGroup = {
  slot: string; count: number; eligible_positions: string[]; streamable: boolean; teams_ranked: number;
  replacement_per_slot: number | null; replacement_note: string | null;
  league_median_per_slot: number | null; league_best_per_slot: number | null; unknown_teams: number[];
};

export type LaCaveat = { code: string; status?: string; text: string };

export type LaTeam = {
  team_id: number; name: string; abbrev: string | null; is_mine: boolean;
  record: { wins: number | null; losses: number | null; ties: number };
  power: { rank: number | null; score: number | null; lineup_strength: number | null; unranked_reason: string | null };
  lineup: { projected_per_week: number | null; known_per_week: number | null; complete: boolean };
  positions: Record<string, LaCell>;
  strongest: string[];
  weakest: string[];
  caveats: LaCaveat[];
};

export type LaStarter = { espn_player_id: number | null; streamer: boolean; name: string | null; position: string | null;
  weeks: number; average_points: number | null };
/** `edge` is null with a `reason` when a horizon week has no projected free agent or the position is unknown. */
export type LaBench = { best_bench: number | null; best_free_agent: number | null; edge: number | null; has_backup: boolean;
  reason: string | null };
export type LaSideCell = LaCell & { starters: LaStarter[]; bench: LaBench | null; depth: number };
export type LaCompareRow = { slot: string; count: number; streamable: boolean; a: LaSideCell; b: LaSideCell;
  difference_per_slot: number | null; leader: "a" | "b" | "even" | null };
export type LaByeWeek = { week: number; status: string; status_text: string; short_slots: string[];
  unconfirmed_slots: string[]; byes: string[]; starters_on_bye: string[] };
export type LaByes = { available: boolean; reason: string | null; weeks: LaByeWeek[]; conflict_weeks: number[];
  schedule_missing_weeks?: number[] };
export type LaNeed = { position: string; rostered: number | null; league_average: number | null; comparison: string | null;
  need_score: number | null; starters_required: number | null; healthy: number | null };
export type LaSide = { team_id: number; name: string; is_mine: boolean; lineup: LaTeam["lineup"]; byes: LaByes;
  needs: { available: boolean; positions: LaNeed[]; scoring_period?: number | null; note?: string } };
export type LaCompare = { a: LaSide; b: LaSide; positions: LaCompareRow[]; leads: { a: number; b: number; even: number };
  shared_conflict_weeks: number[]; caveats: LaCaveat[]; rule: string };

export type LaCompatibility = { contract: string; tool: string; label: string; status: string;
  dimensions: Record<string, { status: string; reason: string | null; missing_input: string | null; action: string | null }> };

export type LeagueAnalyzer = {
  contract: string;
  league: { league_id: number; season: number; name: string; current_week: number };
  week: number | null;
  focus_team_id: number | null;
  compare_team_id: number | null;
  horizon: { key: Horizon; label: string; weeks: number[]; options: { key: Horizon; label: string }[];
             uncertain: boolean; warning: string | null };
  kdst_streaming: boolean;
  projection_source: string | null;
  units: Record<string, string>;
  method: string[];
  freshness: { league_synced_at: string | null; projections_synced_at: string | null };
  compatibility: LaCompatibility | null;
  empty: boolean;
  message: string | null;
  groups: LaGroup[];
  teams: LaTeam[];
  compare: LaCompare | null;
  caveats: LaCaveat[];
};

import { TEAM_SPECIFIC_PARAMS } from "./team-perspective.ts";

export type Horizon = "ros" | "week";
export const PATH = "/league-analyzer";
/** Positions trade discovery can target (its targeted lens takes QB, RB, WR, TE). */
export const TARGETABLE = ["QB", "RB", "WR", "TE"];

type SP = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);
const id = (v: string | undefined): number | null => (v !== undefined && /^\d{1,6}$/.test(v.trim()) ? Number(v.trim()) : null);

/** The view in the URL besides the team: compare team, horizon, position filter, K/D/ST treatment. */
export type LaView = { vs: number | null; horizon: Horizon; pos: string | null; kdst: boolean };

export function readView(sp: SP): LaView {
  const pos = first(sp.pos);
  return {
    vs: id(first(sp.vs)),
    horizon: first(sp.horizon) === "week" ? "week" : "ros",
    // a slot label as the backend names it (QB, D/ST, RB/WR, FLEX, OP); anything else is ignored
    pos: pos && /^[A-Z][A-Z/]{0,7}$/.test(pos) ? pos : null,
    kdst: first(sp.kdst) !== "hold",
  };
}

/** The backend path for a team (null = the default team) and a view. */
export function apiPath(team: number | null, v: LaView): string {
  const q = new URLSearchParams();
  if (team !== null) q.set("team_id", String(team));
  if (v.vs !== null) q.set("vs", String(v.vs));
  if (v.horizon !== "ros") q.set("horizon", v.horizon);
  if (!v.kdst) q.set("kdst_streaming", "false");
  const s = q.toString();
  return s ? `/api/league-analyzer?${s}` : "/api/league-analyzer";
}

/** This page's URL for a team and a view, with `over` applied (null clears a value). Defaults are left out. */
export function viewHref(team: number | null, v: LaView, over: Partial<LaView> = {}): string {
  const n = { ...v, ...over };
  const q = new URLSearchParams();
  if (team !== null) q.set("team", String(team));
  if (n.vs !== null && n.vs !== team) q.set("vs", String(n.vs));
  if (n.horizon !== "ros") q.set("horizon", n.horizon);
  if (n.pos) q.set("pos", n.pos);
  if (!n.kdst) q.set("kdst", "hold");
  const s = q.toString();
  return s ? `${PATH}?${s}` : PATH;
}

export const scoutingHref = (teamId: number) => `/managers/${teamId}`;
export const valueHref = (teamId: number) => `/my-team/value?team=${teamId}`;
export const powerCardHref = (teamId: number) => `/power-rankings/${teamId}`;

/** Trade ideas between the focus team and another manager. */
export function tradesWithHref(focus: number, opponent: number): string {
  return `/trades/discover?team=${focus}&opponent=${opponent}`;
}

/** Trade discovery aimed at one of the team's positions, when that lens can target it (else null). */
export function targetHref(team: number, slot: string): string | null {
  return TARGETABLE.includes(slot) ? `/trades/discover?team=${team}&mode=targeted&pos=${encodeURIComponent(slot)}` : null;
}

/** "1st", "2nd", "3rd", "11th". */
export function ordinal(n: number): string {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 13) return `${n}th`;
  return `${n}${({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[n % 10] ?? "th"}`;
}

/** "+2.4" / "-0.8" / "0.0", or null for a missing value (the caller renders a dash with the reason). */
export function signedText(v: number | null | undefined, digits = 1): string | null {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  const s = v.toFixed(digits);
  const zero = (0).toFixed(digits);
  return s === `-${zero}` ? zero : v > 0 && s !== zero ? `+${s}` : s;
}

/** "3rd of 10", or null when the cell is not ranked. */
export function rankText(c: Pick<LaCell, "rank" | "of">): string | null {
  return c.rank === null ? null : `${ordinal(c.rank)} of ${c.of}`;
}

/** Words for a cell that has no value: never a number, never "last". */
export function unknownLabel(c: Pick<LaCell, "status">): string {
  return c.status === "unverified" ? "Unverified" : "Unknown";
}

/** "4-1" or "4-1-1"; a dash without a record. */
export function recordText(r: { wins: number | null; losses: number | null; ties?: number | null }): string {
  if (r.wins === null || r.losses === null) return "—";
  return `${r.wins}-${r.losses}${r.ties ? `-${r.ties}` : ""}`;
}

/** Teams ordered for a position view: ranked by that group's rank, then the unknown ones (backend order kept). */
export function sortForPosition(teams: LaTeam[], slot: string | null): LaTeam[] {
  if (!slot) return teams;
  const ranked = teams.filter((t) => t.positions[slot]?.rank != null)
    .sort((a, b) => (a.positions[slot].rank! - b.positions[slot].rank!) || a.team_id - b.team_id);
  return [...ranked, ...teams.filter((t) => t.positions[slot]?.rank == null)];
}

/** The view with its position filter kept only when it names one of this league's slot groups (`?pos=OP` in a league
 *  without OP, from a stale or another league's link, is cleared: the title, the All chip and every link then agree). */
export function knownView(v: LaView, groups: Pick<LaGroup, "slot">[]): LaView {
  return v.pos !== null && !groups.some((g) => g.slot === v.pos) ? { ...v, pos: null } : v;
}

/** The groups a view shows: one when a position filter names a group of this league, else all. */
export function visibleGroups(groups: LaGroup[], pos: string | null): LaGroup[] {
  const hit = pos ? groups.filter((g) => g.slot === pos) : [];
  return hit.length ? hit : groups;
}

/** Bar width (0-100) for a score; null renders no bar. */
export function barWidth(score: number | null | undefined): number | null {
  if (score === null || score === undefined || Number.isNaN(score)) return null;
  return Math.max(0, Math.min(100, score));
}

/** "Team A leads at 3 positions, Team B at 2, even at 1" over the compared, non-streamable positions. */
export function leadsText(c: Pick<LaCompare, "a" | "b" | "leads">, plural: (n: number, w: string) => string): string {
  const parts = [`${c.a.name} leads at ${c.leads.a} ${plural(c.leads.a, "position")}`, `${c.b.name} at ${c.leads.b}`];
  if (c.leads.even) parts.push(`even at ${c.leads.even}`);
  return parts.join(", ");
}

// ---------- Error and empty states ----------

/** Params dropped when a visitor without a team picks one from a shared compare link: the usual team-specific ones,
 *  except `vs`, so the comparison the link was shared for survives the pick. */
export const PICK_TEAM_RESET: readonly string[] = TEAM_SPECIFIC_PARAMS.filter((p) => p !== "vs");

/** Every way the analyzer can fail to show a matrix, decided from the API's X-FI-Error-Code (never from its wording). */
export type LaErrorState =
  | { kind: "unknown_team"; team: number | null }            // the focus team is not in this league
  | { kind: "unknown_vs"; vs: number | null }                // the focus is fine; only the compared team is wrong
  | { kind: "unknown_teams"; team: number | null; vs: number | null }
  | { kind: "same_team" }                                    // the same team on both sides (422)
  | { kind: "no_own_team" }                                  // a compare link opened by a visitor without a team
  | { kind: "not_synced" }
  | { kind: "backend" };                                     // anything else (offline, schema, an unexpected status)

const CODES = new Set(["unknown_team", "unknown_vs", "unknown_teams", "same_team", "no_own_team", "not_synced"]);

/** The page's state for a failed analyzer call, from the route's code and the team ids the URL asked for. A failure
 *  without a known code (offline, an older backend, a 404 from the league itself) is a backend problem, never a guess
 *  from the message text. */
export function errorState(res: { status?: number; code?: string; kind?: string }, team: number | null, view: Pick<LaView, "vs">): LaErrorState {
  const code = res.code && CODES.has(res.code) ? res.code : null;
  switch (code) {
    case "unknown_team": return { kind: "unknown_team", team };
    case "unknown_vs": return { kind: "unknown_vs", vs: view.vs };
    case "unknown_teams": return { kind: "unknown_teams", team, vs: view.vs };
    case "same_team": return { kind: "same_team" };
    case "no_own_team": return { kind: "no_own_team" };
    case "not_synced": return { kind: "not_synced" };
    default: return { kind: "backend" };
  }
}
