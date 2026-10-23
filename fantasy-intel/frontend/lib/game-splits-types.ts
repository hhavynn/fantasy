// GET /api/research/game-splits and /api/research/game-splits/players (game-splits.v1, issue #210).
// The backend computes every number; the page only formats, filters and links them.

export type SplitsInputs = {
  player: string;
  teammate: string | null;
  season_from: number;
  week_from: number;
  season_to: number;
  week_to: number;
  as_of: string | null;
};

export type SplitsMetricFormat = "pct" | "num" | "signed";
export type SplitsMetric = { key: string; label: string; format: SplitsMetricFormat };

export type SplitsPlayer = {
  gsis_id: string;
  name: string;
  position: string | null;
  espn_player_id: number | null;
  mapping_status: "verified" | "ambiguous" | "name_match_only" | "unmapped";
};

export type SplitsEnvironment = {
  games: number;
  team_points_pg?: number | null;
  implied_total_pg?: number | null;
  neutral_pass_rate?: number | null;
  offense_plays_pg?: number | null;
  home_games?: number;
  primary_qbs?: { gsis_id: string; name: string; games: number }[];
  opponents?: { team: string; games: number }[];
  /** metric -> games with a known value, when fewer than the group's games */
  coverage?: Record<string, number>;
};

export type SplitsGroup = {
  games: number;
  weeks: { season: number; week: number }[];
  /** The research lab's window summary over the group's games (null = unknown, never zero). */
  metrics: Record<string, number | number[] | Record<string, number> | null> & { games: number; coverage: Record<string, number> };
  environment: SplitsEnvironment;
  reasons?: Record<string, number>;
};

export type SplitsOutcome = "with" | "without" | "excluded" | "not_counted";

export type SplitsWeek = {
  season: number;
  week: number;
  team: string | null;
  opponent: string | null;
  gameday: string | null;
  home: boolean | null;
  final: boolean | null;
  team_points: number | null;
  primary_qb: string | null;
  primary_qb_name: string | null;
  env: Record<string, number | null>;
  subject: {
    offense_pct: number | null; targets: number | null; carries: number | null; opportunities: number | null;
    routes: number | null; xfp: number | null; fantasy_points_ppr: number | null;
  } | null;
  teammate: {
    roster: { team: string; status: string }[];
    usage_team: string | null;
    offense_pct: number | null;
    reference_share: number | null;
    reference_games: number;
  } | null;
  outcome: SplitsOutcome;
  reason: string | null;
  reason_label: string;
};

export type SplitsRules = {
  min_group_games: number;
  small_group_games: number;
  partial_share_ratio: number;
  partial_reference_min_games: number;
  max_seasons: number;
  weeks: [number, number];
  reasons: Record<string, string>;
  scoring_basis: string;
};

type SplitsBase = {
  version: string;
  inputs: SplitsInputs;
  cache_key: string;
  message?: string | null;
};

export type SplitsNoData = SplitsBase & { status: "no_data"; missing_tables: string[] };

export type SplitsNeedsTeammate = SplitsBase & {
  status: "needs_teammate";
  rules: SplitsRules;
  confounding: string[];
  metrics: SplitsMetric[];
  player: SplitsPlayer;
  teammate: null;
  teammate_options: { gsis_id: string; name: string; position: string; teams: string[]; games_together_listed: number }[];
};

export type SplitsResult = SplitsBase & {
  status: "ok" | "low_n";
  rules: SplitsRules;
  confounding: string[];
  metrics: SplitsMetric[];
  player: SplitsPlayer;
  teammate: SplitsPlayer & { teams_with_player: string[] };
  window: {
    start: { season: number; week: number };
    end: { season: number; week: number };
    as_of: string | null;
    weeks_listed: number;
    usage_through_week: number | null;
  };
  groups: { with: SplitsGroup; without: SplitsGroup };
  differences: Record<string, number | null> | null;
  abstention: { reason: "low_n"; message: string } | null;
  cautions: string[];
  counts: {
    with: number; without: number; excluded: number; excluded_by_reason: Record<string, number>;
    not_counted: number; not_counted_by_reason: Record<string, number>;
  };
  weeks: SplitsWeek[];
};

export type GameSplitsResponse = SplitsNoData | SplitsNeedsTeammate | SplitsResult;

export type SplitPlayersResponse = {
  version: string;
  season: number | null;
  status: "ok" | "empty" | "no_data";
  players: { gsis_id: string; name: string; position: string; team: string | null; games: number }[];
  truncated?: boolean;
  seasons?: number[];
  message?: string;
};
