// Weekly League Recap: response types for GET /api/weekly-recap and pure view helpers (URL state,
// labels, chart rows). Nothing here computes a new statistic. No `@/` value imports, so it runs
// under the unit-test runner.
import type { BarDatum } from "@/components/charts";

export type WrResult = "HOME" | "AWAY" | "TIE";
export type WrLineupStatus = "STARTER" | "BENCH" | "IR" | "UNROSTERED" | "MIXED";

export type WrTeamScore = { team_id: number; name: string; abbrev: string | null; points: number | null };

export type WrMatchup = {
  matchup_id: number;
  matchup_period: number;
  playoff_tier: string | null;
  home_team: WrTeamScore;
  away_team: WrTeamScore;
  winner: WrResult;
  is_tie: boolean;
  margin: number | null;
};

export type WrGame = {
  matchup_id: number;
  winner_team_id: number;
  winner_name: string;
  loser_team_id: number;
  loser_name: string;
  margin: number | null;
  is_tie: boolean;
  score_summary: string;
};

export type WrAward<T> = { award_name: string; description: string; winners: T[]; is_tied: boolean; omitted_reason?: string };

export type WrAwards = {
  highest_score: WrAward<WrTeamScore>;
  biggest_blowout: WrAward<WrGame>;
  closest_matchup: WrAward<WrGame>;
  highest_scoring_loser: WrAward<WrTeamScore>;
  biggest_upset: WrAward<never>;
};

export type WrPlayer = {
  espn_player_id: number;
  full_name: string;
  position: string | null;
  pro_team: string | null;
  points: number;
  fantasy_team_id: number | null;
  fantasy_team_name: string | null;
  lineup_status: WrLineupStatus;
};

export type WrMovePlayer = { player_id: number; player_name: string; position: string | null };

export type WrMove = WrMovePlayer & {
  team_id: number | null;
  team_name: string | null;
  type: string;
  processed_at: string | null;
  scoring_period: number;
  bid_amount?: number | null;
};

export type WrTrade = {
  trade_id: string;
  processed_at: string | null;
  scoring_period: number;
  placed_by: "transaction" | "activity_date";
  sides: { team_id: number | null; team_name: string | null; received: WrMovePlayer[] }[];
};

export type WrActivity = { total_moves: number; adds: WrMove[]; drops: WrMove[]; trades: WrTrade[]; unplaced_trades: number };

export type WrStanding = {
  rank: number;
  team_id: number;
  team_name: string;
  abbrev: string | null;
  wins: number;
  losses: number;
  ties: number;
  games: number;
  win_pct: number | null;
  points_for: number | null;
  points_against: number | null;
  weekly_result: "W" | "L" | "T" | null;
  rank_change: number | null;
};

export type WrNotes = {
  pending_matchups?: number;
  byes?: number;
  missing_scores?: number;
  rostered_without_points?: number;
  partial_period_players?: number;
  lineup_snapshot_weeks?: number[];
  weeks_unmapped?: boolean;
  espn_record_check?: { compared: boolean; mismatched_team_ids: number[] } | null;
};

export type WeeklyRecap = {
  season: number;
  league_id: number;
  league_name: string | null;
  selected_week: number | null;
  available_weeks: number[];
  available_periods: { matchup_period: number; weeks: number[] | null }[];
  is_completed_week: boolean;
  scoring_periods: number[];
  matchups: WrMatchup[];
  awards: WrAwards | null;
  standout_players: WrPlayer[];
  bench_standouts: WrPlayer[];
  unrostered_standouts: WrPlayer[];
  league_activity: WrActivity | null;
  standings: WrStanding[];
  standings_through: number | null;
  rules: { awards: string; players: string; standings: string; trades: string };
  notes: WrNotes;
};

// ------------------------------------------------------------------ URL state --

export const PLAYER_VIEWS = ["top", "bench", "unrostered"] as const;
export const MOVE_VIEWS = ["adds", "drops", "trades"] as const;
export type PlayerView = (typeof PLAYER_VIEWS)[number];
export type MoveView = (typeof MOVE_VIEWS)[number];
export type RecapQuery = { week: number | null; players: PlayerView; moves: MoveView };

const first = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

function pick<T extends string>(v: string | undefined, allowed: readonly T[]): T {
  return (allowed as readonly string[]).includes(v ?? "") ? (v as T) : allowed[0];
}

/** Read `?week=&players=&moves=`; anything unknown falls back to the default (latest week, first tab). */
export function parseRecapQuery(sp: Record<string, string | string[] | undefined>): RecapQuery {
  const w = Number(first(sp.week));
  return {
    week: Number.isInteger(w) && w >= 1 ? w : null,
    players: pick(first(sp.players), PLAYER_VIEWS),
    moves: pick(first(sp.moves), MOVE_VIEWS),
  };
}

/** The backend path for a query: only the week travels; tabs are view state. */
export function backendPath(q: RecapQuery): string {
  return q.week === null ? "/api/weekly-recap" : `/api/weekly-recap?week=${q.week}`;
}

/** Page link for a query with `patch` applied. Default tabs are left out, so links stay short. */
export function recapHref(q: RecapQuery, patch: Partial<RecapQuery> = {}): string {
  const n = { ...q, ...patch };
  const p = new URLSearchParams();
  if (n.week !== null) p.set("week", String(n.week));
  if (n.players !== PLAYER_VIEWS[0]) p.set("players", n.players);
  if (n.moves !== MOVE_VIEWS[0]) p.set("moves", n.moves);
  const s = p.toString();
  return s ? `/weekly-recap?${s}` : "/weekly-recap";
}

// ---------------------------------------------------------------------- labels --

/** "Week 5", "Weeks 15–16" for a multi-week period, or "Period 5" when the weeks are unknown. */
export function weekLabel(weeks: readonly number[] | null | undefined, period: number): string {
  if (!weeks || weeks.length === 0) return `Period ${period}`;
  return weeks.length > 1 ? `Weeks ${weeks[0]}–${weeks[weeks.length - 1]}` : `Week ${weeks[0]}`;
}

/** "4-1" or "3-1-1" (ties only when there are some). */
export function recordText(r: { wins: number; losses: number; ties: number }): string {
  return `${r.wins}-${r.losses}${r.ties ? `-${r.ties}` : ""}`;
}

/** "A", "A and B", "A, B and C". */
export function joinNames(names: readonly string[]): string {
  if (names.length <= 1) return names[0] ?? "";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** Points with two decimals; `null` gives "—". */
export function pointsText(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v.toFixed(2);
}

export const STATUS_LABEL: Record<WrLineupStatus, string> = {
  STARTER: "Started", BENCH: "Bench", IR: "IR", UNROSTERED: "Unrostered", MIXED: "Changed mid-period",
};

export const MOVE_TYPE_LABEL: Record<string, string> = {
  WAIVER: "Waiver", FREEAGENT: "Free agent", ROSTER: "Release", TRADE_ACCEPT: "Trade clean-up",
};

/** Each team's side of a trade as one line: "Team A gets X, Y". */
export function tradeLines(t: WrTrade): string[] {
  return t.sides.map((s) => `${s.team_name ?? "Unknown team"} gets ${s.received.map((p) => p.player_name).join(", ")}`);
}

/** The won/lost/tied side of a matchup for one team, for the W/L/T tag. */
export function sideResult(m: WrMatchup, side: "home" | "away"): "W" | "L" | "T" {
  if (m.is_tie) return "T";
  return (m.winner === "HOME") === (side === "home") ? "W" : "L";
}

/**
 * One bar per team score, highest first (ties by name). Award winners get the default tone so they
 * stand out; everyone else is muted. Missing scores stay `null` (no bar).
 */
export function scoreBars(matchups: readonly WrMatchup[], highlight: ReadonlySet<number>): BarDatum[] {
  const teams = matchups.flatMap((m) => [m.home_team, m.away_team]);
  return teams
    .sort((a, b) => (b.points ?? -Infinity) - (a.points ?? -Infinity) || a.name.localeCompare(b.name))
    .map((t) => ({ label: t.name, value: t.points, tone: highlight.has(t.team_id) ? "default" : "muted" }));
}
