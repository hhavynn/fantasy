// League Power Rankings (GET /api/power-rankings, contract power-rankings/v1): types and pure view helpers.
// No Next, no fetch, so node --test covers them. The backend is authoritative: nothing here re-scores a team.

export type PrComponentKey = "strength" | "depth" | "replacement" | "all_play";
export const COMPONENT_KEYS: PrComponentKey[] = ["strength", "depth", "replacement", "all_play"];

export type PrComponentMeta = { key: PrComponentKey; label: string; unit: string; weight: number; description: string };

export type PrComponent = {
  value: number | null;
  score: number | null;           // league-relative 0-100 (best ranked team 100, worst 0)
  rank: number | null;
  previous_value: number | null;
  previous_score: number | null;
};

export type PrSlotRank = { slot: string; rank: number; average: number | null };
export type PrPositionEdge = {
  position: string; edge: number | null; best_bench: number | null; best_free_agent: number | null; has_backup: boolean;
};
export type PrKeyPlayer = { espn_player_id: number; name: string; position: string; weeks_started: number; marginal: number | null; recovered: number | null };

export type PrTeam = {
  team_id: number;
  name: string;
  abbrev: string | null;
  is_mine: boolean;
  rank: number | null;
  previous_rank: number | null;
  movement: number | null;        // places gained since last week (positive = rose)
  power_score: number | null;
  previous_power_score: number | null;
  record: { wins: number | null; losses: number | null; ties: number };
  points_for: number | null;
  components: Record<PrComponentKey, PrComponent>;
  all_play: { wins: number; losses: number; ties: number; pct: number | null };
  depth: { share: number | null; starter_weeks: number; key_players: PrKeyPlayer[] } | null;
  replacement: { positions: PrPositionEdge[]; covered: number; of: number } | null;
  positions: { strongest: PrSlotRank[]; weakest: PrSlotRank[] } | null;
  unranked_reason: string | null;
};

export type PrPrevious =
  | { available: true; week: number; reason: null; projection_snapshot: string | null; projection_synced_at: string | null;
      roster_week: number; comparable_weeks: number[]; all_play_periods: number[]; weights: Record<PrComponentKey, number> }
  | { available: false; week: number | null; reason: string | null };

export type PowerRankings = {
  contract: string;
  league: { league_id: number; season: number; name: string; current_week: number; last_regular_season_week: number | null;
            final_scoring_period: number };
  focus_team_id: number | null;
  kdst_streaming: boolean;
  week: number | null;
  empty: boolean;
  message: string | null;
  components: PrComponentMeta[];
  weights: Record<PrComponentKey, number>;
  method: string[];
  teams: PrTeam[];
  window?: { weeks: number[]; comparable_weeks: number[]; excluded_weeks: { week: number; incomplete_teams: string[] }[];
             note: string | null; all_play_periods: number[] };
  previous: PrPrevious;
  freshness: { league_synced_at: string | null; projections_synced_at: string | null };
};

export const PATH = "/power-rankings";

export function cardHref(teamId: number): string {
  return `${PATH}/${teamId}`;
}

/** "1st", "2nd", "3rd", "11th", "12th", "21st". */
export function ordinal(n: number): string {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 13) return `${n}th`;
  return `${n}${({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[n % 10] ?? "th"}`;
}

/** "4-1" or "4-1-1"; a dash when ESPN has no record. */
export function recordText(r: { wins: number | null; losses: number | null; ties?: number | null }): string {
  if (r.wins === null || r.losses === null) return "—";
  return `${r.wins}-${r.losses}${r.ties ? `-${r.ties}` : ""}`;
}

/** "62%" for an all-play winning percentage. */
export function pctText(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`;
}

/** "Up 2 from 5th", "Down 1 from 3rd", "No change", or null without a previous rank. */
export function movementText(t: Pick<PrTeam, "movement" | "previous_rank">): string | null {
  if (t.movement === null || t.previous_rank === null) return null;
  if (t.movement === 0) return `No change from ${ordinal(t.previous_rank)}`;
  return `${t.movement > 0 ? "Up" : "Down"} ${Math.abs(t.movement)} from ${ordinal(t.previous_rank)}`;
}

export type Movers = { rise: PrTeam | null; fall: PrTeam | null; steady: boolean };

/** The biggest rise and the biggest fall (ties: the better current rank). `steady` = every rank held. */
export function biggestMovers(teams: PrTeam[]): Movers {
  const moved = teams.filter((t) => t.movement !== null && t.rank !== null);
  const byRank = (a: PrTeam, b: PrTeam) => (a.rank ?? 0) - (b.rank ?? 0);
  const rises = moved.filter((t) => (t.movement ?? 0) > 0).sort((a, b) => (b.movement ?? 0) - (a.movement ?? 0) || byRank(a, b));
  const falls = moved.filter((t) => (t.movement ?? 0) < 0).sort((a, b) => (a.movement ?? 0) - (b.movement ?? 0) || byRank(a, b));
  return { rise: rises[0] ?? null, fall: falls[0] ?? null, steady: moved.length > 0 && rises.length === 0 && falls.length === 0 };
}

/** A component's value as display text, in its own unit. */
export function componentValueText(key: PrComponentKey, v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  if (key === "all_play") return pctText(v);
  return v.toFixed(1);
}

/** Short unit after a component value on the team card. */
export const COMPONENT_SHORT_UNIT: Record<PrComponentKey, string> = {
  strength: "pts/wk", depth: "pts recovered", replacement: "pts/wk over waivers", all_play: "all-play",
};

/** Bar width (0-100) for a component score; null renders no bar. */
export function barWidth(score: number | null | undefined): number | null {
  if (score === null || score === undefined || Number.isNaN(score)) return null;
  return Math.max(0, Math.min(100, score));
}

/** The team ranked teams (rank present), in rank order, and the unranked ones after them (backend order kept). */
export function splitRanked(teams: PrTeam[]): { ranked: PrTeam[]; unranked: PrTeam[] } {
  return { ranked: teams.filter((t) => t.rank !== null), unranked: teams.filter((t) => t.rank === null) };
}

/** The team a page focuses: the URL's team when it is in the league, else the backend's focus (yours). */
export function focusTeam(d: Pick<PowerRankings, "teams" | "focus_team_id">): PrTeam | null {
  return d.teams.find((t) => t.team_id === d.focus_team_id) ?? null;
}

/** "45%" weight label. */
export function weightText(w: number | null | undefined): string {
  return w === null || w === undefined ? "—" : `${Math.round(w * 100)}%`;
}
