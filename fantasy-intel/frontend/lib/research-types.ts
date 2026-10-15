// Response shapes of /api/research/* (backend/app/analytics/research.py). Every number may be null:
// null means unknown (source missing, no denominator), never zero.

export type ResearchPosition = "WR" | "RB" | "TE";
export type ResearchWindow = "season" | "last3" | "last5";

type N = number | null;

export type WindowSummary = {
  games: number;
  weeks: number[];
  coverage: Record<string, number>;
  xfp_total?: number;
  ppr_total?: number;
  xfp_pg?: number;
  ppr_pg?: number;
  fpoe_pg?: number;
  snap_share?: N;
  targets_pg?: N;
  receptions_pg?: N;
  opportunities_pg?: N;
  rz_targets_pg?: N;
  rz_targets_total?: N;
  target_share?: N;
  air_yards_pg?: N;
  air_yards_share?: N;
  adot?: N;
  wopr?: N;
  carries_pg?: N;
  designed_rushes_pg?: N;
  rz_carries_pg?: N;
  gl_carries_pg?: N;
  backfield_rush_share?: N;
  team_rush_share?: N;
  rz_carries_total?: N;
  gl_carries_total?: N;
  routes_pg?: N;
};

export type ProductionGroup = {
  key: string;
  label: string;
  gap?: string;
  high_opportunity?: boolean;
  production?: "below" | "above" | "inline";
};

export type Confidence = { level: "HIGH" | "MEDIUM" | "LOW" | "INSUFFICIENT"; label: string; n_recent: number; n_baseline: number };

export type TrendDetail = {
  metric: string;
  recent_avg: N;
  baseline_avg: N;
  delta: N;
  label: string;
  n_recent: number;
  n_baseline: number;
  recent_weeks: number[];
  baseline_weeks: number[];
  threshold: number;
};

export type Momentum = {
  xfp_delta: N;
  ppr_delta: N;
  xfp_recent: N;
  xfp_baseline: N;
  ppr_recent: N;
  ppr_baseline: N;
  recent_weeks: number[];
  baseline_weeks: number[];
  label: string;
  reading: string | null;
  confidence: Confidence;
  metrics: Record<string, TrendDetail>;
  volume_metric: string;
  share_metric: string;
};

export type Market = {
  market_signal: string | null;
  opportunity_tier: string | null;
  role_trend: string | null;
  value_gap_label: string | null;
  production_sustainability: string | null;
  as_of_week: N;
  games: N;
  fp_minus_xfp: N;
};

export type Injury = {
  espn_status: string | null;
  nfl_report: { week: number; status: string | null; injury: string | null; practice: string | null } | null;
};

export type ResearchPlayer = {
  gsis_id: string;
  name: string;
  position: ResearchPosition;
  team: string | null;
  espn_player_id: number | null;
  mapping_status: "verified" | "ambiguous" | "name_match_only" | "unmapped";
  games_played: number;
  excluded_games: { weeks: number[]; reason: string } | null;
  last_game_week: N;
  window: WindowSummary;
  momentum: Momentum;
  market: Market | null;
  injury: Injury | null;
  group: ProductionGroup;
  research_groups: string[];
  percentiles?: Record<string, number>;
};

export type DataFreshness = {
  season: number;
  latest_week: N;
  xfp_through_week: N;
  weeks_with_data: number[];
  weeks_missing_xfp: number[];
  nfl_synced_at: string | null;
  analytics_computed_at: string | null;
  route_rows: number;
  scoring_basis: string;
};

export type GroupInfo = { title: string; rule: string; caveat: string };

export type ResearchFilters = {
  position: ResearchPosition;
  window: ResearchWindow;
  min_games: number;
  min_volume: number;
  team: string | null;
  volume_metric: "targets_pg" | "opportunities_pg";
  volume_label: string;
};

export type OpportunityResponse = {
  season: number | null;
  status: "ok" | "empty" | "no_data";
  message?: string;
  filters: ResearchFilters;
  defaults: { volume_metric: string; volume_label: string; min_volume: number; min_games: number };
  windows: Record<ResearchWindow, string>;
  high_opportunity_xfp: number;
  value_gap_margin: { abs: number; rel: number; min_games: number };
  groups_info: Record<string, GroupInfo>;
  data: DataFreshness | null;
  players: ResearchPlayer[];
  teams: string[];
  pool: { at_position: number; shown: number; excluded: Record<string, number> };
};

export type WeekState = {
  week: number;
  state: "played" | "missing_xfp" | "no_offensive_snaps" | "bye" | "did_not_play" | "unknown";
  label: string;
  team: string | null;
  opponent: string | null;
  xfp?: N;
  fantasy_points_ppr?: N;
  fpoe?: N;
  offense_pct?: N;
  targets?: N;
  target_share?: N;
  air_yards_share?: N;
  air_yards?: N;
  carries?: N;
  designed_rushes?: N;
  backfield_rush_share?: N;
  rz_targets?: N;
  rz_carries?: N;
  gl_carries?: N;
  opportunities?: N;
  routes?: N;
};

export type PlayerResearch = {
  season: number;
  data: DataFreshness;
  player: Pick<ResearchPlayer, "gsis_id" | "name" | "position" | "team" | "espn_player_id" | "mapping_status"
    | "games_played" | "last_game_week" | "excluded_games" | "market" | "injury">;
  windows: Record<ResearchWindow, { window: WindowSummary; group: ProductionGroup; research_groups: string[] }>;
  momentum: Momentum;
  timeline: WeekState[];
  league: { availability_status: string | null; on_team_id: number | null; my_team_id: number | null; is_mine: boolean } | null;
  high_opportunity_xfp: number;
  groups_info: Record<string, GroupInfo>;
};
