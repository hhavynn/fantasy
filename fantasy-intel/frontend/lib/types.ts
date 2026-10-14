// Shapes returned by the FastAPI backend (only the fields the UI reads).

export type SyncInfo = {
  last_success: Record<string, string | null>;
  latest_runs: { source: string; status: string; message: string | null; finished_at: string | null }[];
};

export type League = {
  league_id: number; season: number; name: string; size: number; scoring_type: string;
  reception_points: number | null; is_faab: boolean; faab_budget: number | null; current_week: number;
  roster_slots: Record<string, number>; trade_deadline: string | null; synced_at: string;
};

export type Team = {
  team_id: number; name: string; abbrev: string; owner_names: string | null; wins: number; losses: number;
  ties: number; points_for: number; points_against: number; playoff_seed: number | null;
  waiver_rank: number | null; faab_spent: number | null; acquisitions: number | null; drops: number | null;
  trades: number | null; move_to_ir: number | null; is_mine: boolean;
};

export type PlayerCore = {
  espn_player_id: number; full_name: string; position: string; pro_team: string | null;
  injury_status: string | null; availability_status: string | null; on_team_id: number | null;
  percent_owned: number | null; percent_change: number | null; season_points: number | null;
  season_avg_points: number | null; gsis_id: string | null; match_method: string | null;
  match_confidence: string | null; games: number | null; opportunity_score: number | null;
  production_score: number | null; usage_trend_score: number | null; trend_label: string | null;
  fp_per_game: number | null; xfp_per_game: number | null; fp_minus_xfp: number | null;
  value_gap_label: string | null; classification: string | null; value_score: number | null;
  fantasy_team?: string | null;
};

export type RosterPlayer = PlayerCore & {
  lineup_slot: string; lineup_slot_id: number; is_starter: boolean; acquisition_type: string | null;
  week_points: number | null; week_projection: number | null; on_bye: boolean | null;
  nfl_injury: { week: number; report_status: string | null; report_primary_injury: string | null; practice_status: string | null } | null;
};

export type Need = {
  position: string; starters_required: number; rostered: number; healthy: number; unavailable: number;
  starter_value: number | null; need_score: number;
  details: { depth_target: number; shortage: number; quality_gap: number; league_avg_starter_value: number | null };
};

export type Move = {
  transaction_id: string; type: string; date: string | null; scoring_period: number | null; bid_amount: number | null;
  item_type: string; espn_player_id: number; full_name: string | null; position: string | null;
  team_name: string | null; from_team_name: string | null; to_team_name: string | null;
};

export type Classification = {
  dimension: string; label: string; metric: string; value: number | null; league_mean: number | null;
  league_std: number | null; z_score: number | null; sample_size: number; min_sample: number;
  confidence: string; rule: string;
};

export type Metric = {
  metric: string; value: number | null; numerator: number | null; denominator: number | null;
  sample_size: number | null; unit?: string; description?: string;
};

export type UsageWeek = {
  week: number; team: string; opponent: string | null; offense_snaps: number | null; offense_pct: number | null;
  targets: number | null; receptions: number | null; receiving_yards: number | null; air_yards: number | null;
  target_share: number | null; adot: number | null; carries: number | null; rushing_yards: number | null;
  rush_share: number | null; yards_per_carry: number | null; rz_targets: number | null; rz_carries: number | null;
  gl_carries: number | null; opportunities: number | null; fantasy_points_ppr: number | null;
  league_points: number | null; xfp: number | null; receiving_tds: number | null; rushing_tds: number | null;
  stats_source: string; wopr: number | null;
};

export type TrendInfo = {
  metric: string; recent_avg: number | null; baseline_avg: number | null; delta: number | null; threshold: number;
  label: string; recent_weeks: number[]; baseline_weeks: number[];
};
