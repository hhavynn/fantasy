import type { RosterLegality } from "./lineup-coverage.ts";

// Shapes returned by the FastAPI backend (only the fields the UI reads).

export type SyncInfo = {
  last_success: Record<string, string | null>;
  latest_runs: { source: string; status: string; message: string | null; finished_at: string | null }[];
};

export type League = {
  league_id: number; season: number; name: string; size: number; scoring_type: string;
  reception_points: number | null; is_faab: boolean; faab_budget: number | null; current_week: number;
  roster_slots: Record<string, number>; trade_deadline: string | null; synced_at: string;
  /** A week-based trade deadline (Sleeper); null when the provider has none. Absent from older backends. */
  trade_deadline_week?: number | null;
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
  opportunity_basis: string | null; opportunity_tier: string | null; role_trend: string | null;
  market_signal: string | null;
  production_sustainability: string | null;   // production vs xFP baseline: regression evidence, not a trade call
  fantasy_team?: string | null;
  // 0–100.0 ratings (ratings-100.v1). Null = not computed; absent on a database written before the scale.
  opportunity_score_100?: number | null; production_score_100?: number | null; value_score_100?: number | null;
  rating_scale_version?: string | null;
};

export type RosterPlayer = PlayerCore & {
  lineup_slot: string; lineup_slot_id: number; is_starter: boolean; acquisition_type: string | null;
  week_points: number | null; week_projection: number | null; on_bye: boolean | null;
  availability: Availability;
  nfl_injury: { week: number; report_status: string | null; report_primary_injury: string | null; practice_status: string | null } | null;
};

/** status: AVAILABLE | QUESTIONABLE | DOUBTFUL | OUT | SUSPENDED | IR | TAXI (Sleeper taxi squad: cannot start). */
export type Availability = { status: string; weight: number; sources: string[] };

export type Need = {
  position: string; starters_required: number; rostered: number; healthy: number; unavailable: number;
  effective_available: number | null; starter_value: number | null; need_score: number;
  details: {
    depth_target: number; shortage: number; quality_gap: number; league_avg_starter_value: number | null;
    /** "partial": a competing player has no historical value, so quality_gap is not measured (quality_gap_known false)
     * and need_score uses roster shortage only. */
    quality_gap_known?: boolean; value_status?: "complete" | "partial"; value_note?: string | null;
    fixed_slots?: number; flex_slots_filled?: number;
    availability?: { espn_player_id: number; name: string | null; status: string; weight: number }[];
  };
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
  team_rush_share: number | null; backfield_rush_share: number | null; rush_attempts: number | null;
  designed_rushes: number | null; rushing_success_rate: number | null; explosive_run_rate: number | null;
  ybc_per_att: number | null; yac_per_att: number | null; broken_tackles: number | null;
  ftn_read1_targets: number | null; ftn_read1_target_share: number | null; designed_targets: number | null;
  checkdown_targets: number | null; ngs_rush_yoe_per_att: number | null; ngs_avg_separation: number | null;
  air_yards_share: number | null; yards_per_carry: number | null; rz_targets: number | null; rz_carries: number | null;
  gl_carries: number | null; opportunities: number | null; fantasy_points_ppr: number | null;
  league_points: number | null; xfp: number | null; receiving_tds: number | null; rushing_tds: number | null;
  stats_source: string; wopr: number | null;
};

export type TrendInfo = {
  metric: string; recent_avg: number | null; baseline_avg: number | null; delta: number | null; threshold: number;
  label: string; recent_weeks: number[]; baseline_weeks: number[];
};

export type ContextLabels = {
  role: string; role_trend: string; efficiency: string; scoring_opportunity: string;
  offensive_environment: string; matchup: string; data_confidence: string; run_blocking?: string;
};

export type ContextEvidence = {
  games_recent: number; recent_weeks: number[]; games_season: number;
  snap_pct: number | null; target_share: number | null; air_yards_share: number | null; wopr: number | null;
  adot: number | null;
  designed_targets: number | null; team_rush_share: number | null; backfield_rush_share: number | null;
  rz_targets_pg: number | null; rz_carries_pg: number | null; gl_carries_recent: number | null;
  designed_rushes_season: number; rushing_success_rate: number | null; explosive_run_rate: number | null;
  ybc_per_att: number | null; yac_per_att: number | null; broken_tackles: number | null;
  ngs_rush_yoe_per_att: number | null; ngs_avg_separation: number | null;
  matchup: { opponent?: string; games?: number; avg_rank?: number; ranks?: Record<string, number | null>; rule?: string };
  team: { proe: number | null; neutral_pass_rate: number | null; sack_rate: number | null; hit_or_sack_rate: number | null;
    pfr_pressure_rate: number | null; next_opponent: string | null; next_implied_total: number | null };
  headlines: string[]; rules: Record<string, string>;
  experimental?: FtnSemantics & { ftn_read1_target_share: number | null; ftn_read1_targets: number | null };
};

export type FtnSemantics = {
  read_semantics_status: string; assumed_primary_read_code: string; reference: string; note: string;
};

export const FTN_READ1_LABEL = "FTN progression read-code 1 share (experimental)";
export const FTN_READ1_NOTE =
  "Experimental. The nflreadr dictionary documents read_thrown \"0\" as the primary read, but observed data conflicts; " +
  "upstream clarification is pending (nflverse/nflreadr#325). Shown for exploration only and never used in scores, " +
  "signals or waiver recommendations. Not route participation.";

export type RouteData = {
  season: Partial<Record<"routes" | "route_participation" | "tprr" | "yprr" | "pass_snaps" | "pass_block_snaps" |
    "pass_block_rate" | "slot_rate" | "wide_rate" | "inline_rate" | "backfield_rate" | "two_minute_route_share" |
    "long_down_route_share" | "short_down_route_share" | "man_tprr" | "zone_tprr", number>>;
  weeks: number[]; providers: string[]; datasets: string[]; source_updated_at: string | null;
  provider_defined: string[]; note: string;
};

export const BASIS_LABEL: Record<string, string> = {
  xfp: "xFP (expected fantasy points)",
  "targets+carries": "targets + carries (xFP unavailable)",
  espn_points: "ESPN league points only",
  unavailable: "no usage data",
};

// ESPN forward projection baseline (ESPN's numbers, unmodified; not our projection).
export type ForwardWeek = {
  week: number; phase: "regular" | "playoff" | null; opponent: string | null; is_home: boolean | null;
  on_bye: boolean | null; espn_projected_points: number | null; projection_available: boolean;
};

export type RosSummary = {
  as_of_week: number; final_week: number; last_regular_season_week: number | null;
  current_week_projection: number | null;
  ros_projected_points: number | null; ros_weeks_expected: number; ros_weeks_available: number; ros_coverage: number | null;
  regular_season_projected_points: number | null; regular_season_weeks_expected: number | null;
  regular_season_weeks_available: number | null;
  playoff_projected_points: number | null; playoff_weeks_expected: number | null; playoff_weeks_available: number | null;
  bye_weeks_remaining: number | null; bye_weeks: number[]; missing_weeks: number[]; projection_source: string;
};

/** Where an "ours" week's number came from: our model, our model's zero (bye / unavailable), ESPN's fallback, or a schedule bye. */
export type OursOrigin = "ours" | "ours_zero" | "espn" | "bye";

/** One remaining regular-season week of the "ours" series (GET /api/players/{id}/forward?source=independent). */
export type OursWeek = {
  week: number; value: number | null; origin: OursOrigin; on_bye: boolean | null;
  run_id: string | null; model_version: string | null; run_created_at: string | null;
  horizon: number | null; forecast_horizon: number | null; projected_from_week: number | null;
  horizon_note: string | null; experimental: boolean;
  /** true: the run's artifact is approved; false: approval could not be verified (shown anyway); null: no run or no rule. */
  artifact_verified?: boolean | null;
};

/** Fantasy Intel's production model for the rest of the regular season. `ros_projected_points` is null unless complete. */
export type OursForward = {
  available: boolean; reason: string | null; label: string; model_version: string; model_versions: string[];
  mixed_models: boolean; weeks: OursWeek[];
  ros_projected_points: number | null; ros_weeks_expected: number; ros_weeks_available: number; ros_complete: boolean;
  fallback_weeks: number[]; missing_weeks: number[]; other_model_weeks: number[]; last_regular_season_week: number | null; run_created_at: string | null;
};

export type ForwardProjection = {
  source: string; note: string; summary: RosSummary; projection_synced_at: string | null;
  weeks: ForwardWeek[]; schedule_available: boolean;
  /** Present only when the request carried source=independent. */
  ours?: OursForward;
};

// V2 Trade Evaluator (POST /api/trades/evaluate). Projected points: ESPN. Trade impact: exact lineup simulation.
export type TradeOptions = {
  league: League;
  evaluation: TradeWindow;
  teams: { team_id: number; name: string; abbrev: string; is_mine: boolean;
    roster: { espn_player_id: number; name: string | null; position: string | null; pro_team: string | null;
      on_ir: boolean; on_taxi?: boolean; injury_status: string | null; ros_projected_points: number | null }[] }[];
  active_capacity: number;
  position_limits: Record<string, number>;
};

export type TradeWindow = {
  // start_week is null when no fantasy scoring period remains (every week has kicked off).
  start_week: number | null; final_week: number; last_regular_season_week: number | null; weeks: number[];
  remaining_weeks?: boolean; uncertain?: boolean; warning?: string | null;
  start_week_overridden?: boolean; earliest_actionable_week?: number | null;
  start_week_reason: { reason?: string; evaluation_start_week?: number | null; espn_current_week?: number;
    current_week_status?: string; uncertain?: boolean; warning?: string | null };
};

/** `streamer`: the K/D/ST waiver streamer at that week's streaming baseline (player_id null, not an empty slot). */
export type LineupSlot = { slot: string; player_id: number | null; name?: string | null; position: string | null;
  points: number | null; is_flex: boolean; streamer?: boolean };
/** The K/D/ST weekly streaming baseline a lineup computation used (enabled false = current K/D/ST held all season). */
export type KdstStreaming = { enabled: boolean; rank?: number; note?: string;
  positions: { position: string; weeks: { week: number; phase: string | null; espn_player_id: number; name: string | null;
    pro_team: string | null; points: number; rank: number; available_projected: number }[] }[] };
export type LineupWeek = { week: number; phase: string | null; projection_complete: boolean;
  known_lineup_points: number; projected_lineup_points: number | null; assignment: LineupSlot[];
  bench: { player_id: number; name?: string | null; position: string | null; points: number | null }[];
  ir: { player_id: number; name?: string | null; position: string | null; points: number | null }[];
  /** Sleeper taxi-squad players (never start); present only for a team that has one. */
  taxi?: { player_id: number; name?: string | null; position: string | null; points: number | null }[];
  missing_projection_ids: number[]; teamless_unprojected_ids: number[];
  /** Starting slots no engine fills: the total is partial (issue #253 G3). */
  unsupported_slots?: string[] };

export type TeamLineupSummary = {
  ros_projected_starting_points: number | null; ros_known_starting_points: number;
  regular_season_projected_starting_points: number | null; playoff_projected_starting_points: number | null;
  ros_weeks_expected: number; ros_weeks_complete: number; missing_weeks: number[]; missing_players: number[];
  /** Starting slots no engine fills: every total above leaves them out, so it is partial (issue #253 G3). */
  unsupported_slots?: string[];
};

export type TradeDeltas = {
  ros_delta: number | null; regular_season_delta: number | null; playoff_delta: number | null;
  known_ros_delta: number; average_weekly_delta: number | null; weeks_improved: number; weeks_worsened: number;
  weeks_unchanged: number; weeks_incomplete: number[]; projection_complete: boolean;
  /** Starting slots no engine fills; a delta is still exact when it moves nobody who can start there. */
  unsupported_slots?: string[];
};

export type FaPick = {
  label?: string; note?: string; player: { espn_player_id: number; name: string | null; position: string | null; pro_team: string | null } | null;
  lineup_points_added?: number | null; known_lineup_points_added?: number; weeks_started?: number[];
};

// Hypothetical free activation of a current-IR player; deliberately optimistic, never canonical.
export type IrActivation = {
  label: string; assumption?: string; espn_player_id: number; weekly_projection: Record<string, number | null>;
  projected_weeks: number[]; unprojected_weeks: number[]; weeks_would_start: number[];
  lineup_points_upside: number | null; known_lineup_points_upside: number;
  regular_season_upside: number | null; playoff_upside: number | null; projection_complete: boolean;
  open_active_roster_spots: number | null; open_active_roster_spot: boolean | null; roster_move_note: string | null;
};
export type IrActivationPlayer = {
  espn_player_id: number; name: string | null; position: string | null; pro_team: string | null;
  injury_status: string | null; traded_away: boolean; before_trade: IrActivation; after_trade: IrActivation | null;
};

export type TradeSide = {
  team_id: number; team_name: string | null; sends: number[]; receives: number[];
  before: TeamLineupSummary; after: TeamLineupSummary; deltas: TradeDeltas;
  weekly: { week: number; phase: string | null; before: number | null; after: number | null; delta: number | null;
    known_delta: number; projection_complete: boolean; entered: number[]; exited: number[] }[];
  lineups: { before: LineupWeek[]; after: LineupWeek[] };
  roster: { active_capacity: number;
    /** null (with every count below) when roster legality is unverified (`legality`): the count may leave players out. */
    active_before: number | null; active_after: number | null;
    open_active_roster_spots: number | null; open_active_roster_spots_before: number | null;
    legality?: RosterLegality;
    forced_drops: { espn_player_id: number; name: string | null; position: string | null; is_incoming: boolean;
      /** He can start only in a slot no engine fills: not valued (chosen only when no other drop is legal). */
      unsupported_slot_player?: boolean }[];
    forced_drop_lineup_cost: number | null; forced_drop_known_lineup_cost: number | null; forced_drop_basis: string | null;
    forced_drop_unverified?: { status: "unverified"; code: string; note: string };
    redundant_incoming_asset: { player_ids: number[]; names: (string | null)[]; lineup_points_saved_by_cutting_it: number; note: string } | null };
  incoming_usage: { espn_player_id: number; name: string | null; position: string | null; weeks_started: number[]; slots: Record<string, number> }[];
  fit_notes: string[];
  hypothetical_fa_fill: FaPick | null;
  ir_activation_sensitivity: { label: string; players: IrActivationPlayer[] };
  incomplete: { missing_weeks: number[]; missing_players: number[] };
};

export type TradePlayer = {
  espn_player_id: number; name: string | null; position: string | null; pro_team: string | null;
  injury_status: string | null; on_ir: boolean; from_team_id: number; to_team_id: number;
  /** Sleeper taxi squad: inactive on his current roster, active after the trade (absent for everyone else). */
  on_taxi?: boolean;
  marginal_value_on_current_team: { roster_marginal_lineup_points: number | null; regular_season_marginal_lineup_points: number | null;
    playoff_marginal_lineup_points: number | null; known_marginal_lineup_points: number; weeks_started: number[]; weeks_expected: number;
    on_ir: boolean; on_taxi?: boolean; ir_activation_sensitivity?: IrActivation };
  ir_activation_sensitivity: IrActivationPlayer | null;
  replacement_context: { replacement: FaPick["player"]; replacement_position: string | null; eligible_positions: string[];
    lineup_points_lost_without_player: number | null; lineup_points_recovered: number | null;
    residual_loss_vs_original: number | null; known_residual_loss_vs_original: number | null; note: string };
  intelligence: { ros_projected_points: number | null; regular_season_projected_points: number | null;
    playoff_projected_points: number | null; opportunity_tier: string | null; role_trend: string | null;
    production_sustainability: string | null; market_signal: string | null; injury_status: string | null;
    context_labels: Partial<ContextLabels> | null; note: string } | null;
};

export type TradeResult = {
  league: { name: string; season: number; current_week: number; final_scoring_period: number };
  evaluation: TradeWindow;
  trade: { shape: string; team_a_id: number; team_b_id: number; team_a_sends: number[]; team_b_sends: number[] };
  team_a: TradeSide; team_b: TradeSide;
  result: { label: string; team_a_ros_delta: number | null; team_b_ros_delta: number | null;
    combined_ros_delta: number | null; min_side_ros_delta: number | null; regular_season_label: string;
    playoff_label: string; note: string };
  players: TradePlayer[];
  ir_warning: string | null;
  ir_assumption?: string;
  manager_context: Record<string, { needs: { position: string; need_score: number }[];
    classifications: Record<string, { label: string; confidence: string }> }>;
  names: Record<string, { name: string | null; position: string | null; pro_team: string | null }>;
  projection_source: string; trade_impact_source: string; assumptions: string[]; limitations: string[];
  kdst_streaming?: KdstStreaming;
};

// ------------------------------------------------------------ trade discovery --

export type DiscoveryDeltas = {
  ros_delta: number | null; regular_season_delta: number | null; playoff_delta: number | null;
  known_ros_delta: number; average_weekly_delta: number | null;
  weeks_improved: number; weeks_worsened: number; weeks_unchanged: number; weeks_incomplete: number[];
  projection_complete: boolean;
};

export type DiscoveryUsage = { espn_player_id: number; name: string | null; position: string | null;
  weeks_started: number[]; slots: Record<string, number> };

export type DiscoverySide = {
  team_id: number; team_name: string | null; deltas: DiscoveryDeltas;
  before: Record<string, number | null>; after: Record<string, number | null>;
  incoming_usage: DiscoveryUsage[]; outgoing_usage: DiscoveryUsage[];
  displaced: { espn_player_id: number; name: string | null; position: string | null; starts_lost: number[]; starts_gained: number[] }[];
  fit_notes: string[];
  forced_drops: { espn_player_id: number; name: string | null; position: string | null; is_incoming: boolean }[];
  forced_drop_lineup_cost: number | null;
  redundant_incoming_asset: { names: string[]; lineup_points_saved_by_cutting_it: number } | null;
  open_active_roster_spots_before: number | null; open_active_roster_spots: number | null; legality?: RosterLegality;
  hypothetical_fa_fill: { player: { espn_player_id: number; name: string | null; position: string | null } | null;
    lineup_points_added?: number | null } | null;
  incomplete: { missing_weeks: number[]; missing_players: number[] };
  ir_activation_sensitivity: { assumption?: string; players: IrActivationPlayer[] };
};

export type ManagerContext = {
  needs: { position: string; need_score: number | null; starters_required: number | null; rostered: number | null; healthy: number | null }[];
  roster_construction: { position: string; count: number | null; league_average: number | null; league_comparison: string }[];
  roster_scoring_period: number;
  trade_history: { completed_trades: number; confidence: string; behavior: string; uneven_player_count_trades: number; rule: string };
  headlines: { text: string; category: string; evidence: string[]; confidence: string }[];
  note: string;
};

export type DiscoveryRow = {
  key: string; shape: string; anchor_team_id: number; opponent_team_id: number; opponent_name: string | null;
  sends: number[]; receives: number[]; mine: DiscoveryDeltas; theirs: DiscoveryDeltas;
  combined_ros_delta: number | null; min_side_ros_delta: number | null;
  forced_drops: { mine: number[]; theirs: number[] }; categories: string[]; simpler_equivalent: string | null;
};

export type DiscoveryResult = DiscoveryRow & {
  consistent_with_manual_evaluator: boolean; result_label: string; regular_season_label: string; playoff_label: string;
  players: { espn_player_id: number; name: string | null; position: string | null; pro_team: string | null;
    injury_status: string | null; on_ir: boolean; on_taxi?: boolean; from_team_id: number; to_team_id: number;
    marginal_value_on_current_team: number | null }[];
  mine_detail: DiscoverySide; theirs_detail: DiscoverySide;
  ir_warning: string | null; ir_assumption: string;
  manager_context: ManagerContext | null; need_notes: string[];
};

export type DiscoverySearch = {
  candidates_considered: number; screened_out_by_upper_bound: number; survivors: number; matching_filters: number;
  fully_evaluated: number; excluded_invalid_roster: number; excluded_missing_data: number; not_qualified: number;
  qualified_found: number; simpler_equivalents_hidden: number; not_evaluated: number;
  not_evaluated_reason: string | null; largest_unevaluated_upper_bound: number | null;
  termination: "all_candidates_evaluated" | "proven_by_upper_bounds" | "evaluation_cap";
  bounds_valid: boolean; exhaustive: boolean; max_exact: number; lineup_solves_stage_a: number;
  seconds: { stage_a: number; stage_b: number }; consistency_checked: number; consistency_failures: number;
};

export type Discovery = {
  league: { league_id: number; season: number; name: string; current_week: number; final_scoring_period: number };
  anchor: { team_id: number; name: string; abbrev: string; is_mine: boolean };
  teams: { team_id: number; name: string; abbrev: string; is_mine: boolean }[];
  evaluation: { start_week: number; final_week: number; last_regular_season_week: number | null; weeks: number[];
    uncertain: boolean; warning: string | null; start_week_reason: string | null };
  query: { team_id: number; shapes: string[]; sort: string; category: string | null; opponent: number | null;
    limit: number; include_equivalents: boolean };
  results: DiscoveryResult[]; search: DiscoverySearch;
  opponents: { opponent_team_id: number; team_name: string | null; best_my_gain: DiscoveryRow | null; best_mutual: DiscoveryRow | null; exhaustive: boolean }[];
  names: Record<string, { name: string | null; position: string | null; pro_team: string | null; on_ir: boolean; on_taxi?: boolean; injury_status: string | null }>;
  categories: Record<string, string>; sorts: string[]; shapes: string[];
  note: string; assumptions: string[]; limitations: string[];
};

// ------------------------------------------------------- schedule intelligence --

export type SiLabel = "permissive" | "middle" | "tougher" | "insufficient" | "unavailable" | "bye";

export type SiMetric = {
  metric: string; label: string; value: number | null; numerator: number | null; denominator: number | null;
  rank: number | null; ranked_defenses: number; band: SiLabel; missing_reason: string | null;
  position_coverage?: number | null;
};

export type SiExclusion = {
  code: "no_score" | "kickoff_unknown" | "fetch_time_unknown" | "fetched_too_early" | "not_in_schedule";
  reason: string;
};

export type SiEvidence = {
  defteam: string; games_before_week: number; through_week: number | null; games: number; weeks_sampled: number[];
  excluded_weeks: number[]; excluded_reasons: Record<string, SiExclusion>; metrics: SiMetric[]; primary_metric: string; label: SiLabel;
  label_text: string; sentence: string;
};

export type SiWeek = {
  week: number; phase: "regular" | "playoffs" | null; matchup_period: number | null; actionable: boolean;
  window_note?: string; status: "scheduled" | "bye" | "schedule_missing" | "team_unknown";
  opponent?: string | null; home?: boolean; kickoff?: string | null; game_presumed_final?: boolean;
  label: SiLabel; label_text: string; reason?: string; evidence?: SiEvidence;
};

export type SiWindow = { weeks: number[]; counts: Record<SiLabel, number>; opponents: (string | null)[] };

export type SiPlayer = {
  espn_player_id: number; name: string | null; position: string; pro_team: string | null;
  injury_status: string | null; on_ir: boolean;
  identity: { gsis_id: string | null; match_confidence: string; nflverse_latest_team: string | null; nflverse_latest_week: number | null };
  notes: string[]; weeks: SiWeek[];
  summary: { next: SiWindow; regular: SiWindow; playoffs: SiWindow; all: SiWindow };
};

// One input's latest refresh: the sync's start (downloads done) and when it finished writing.
export type SiRefresh = { fetched_at: string | null; refreshed_at: string | null };

export type ScheduleIntelligence = {
  league: { league_id: number; season: number; name: string; current_week: number; final_scoring_period: number;
    last_regular_season_week: number | null };
  team: { team_id: number; name: string; abbrev: string; is_mine: boolean } | null;
  teams: { team_id: number; name: string; abbrev: string; is_mine: boolean }[];
  query: { team_id: number | null; position: string; phase: string; player_id: number | null };
  window: { espn_current_week: number; first_actionable_week: number | null; weeks: number[]; remaining_weeks: boolean;
    uncertain: boolean | null; warning: string | null; reason: string | null; regular_season_through: number | null;
    playoff_weeks: number[]; matchup_periods: Record<string, number | null> };
  schedule: { available: boolean; has_scores: boolean; weeks_covered: number[]; games_by_completion: Record<string, number>;
    schedules_fetched_at: string | null; defense_rows_fetched_at: string | null; completion_reference_time: string | null;
    completion_rule: string; completion_limits: string[]; warnings: { code: string; message: string }[] };
  freshness: { season: number; nfl_schedules: SiRefresh | null; nfl_defense_week: SiRefresh | null;
    latest_nfl_sync: (SiRefresh & { refreshed_nfl_schedules: boolean; refreshed_nfl_defense_week: boolean }) | null;
    defense_weeks_loaded: number[]; source: string };
  players: SiPlayer[];
  metrics: Record<string, { label: string; numerator: string; denominator: string; unit: string; definition: string; positions: string[] }>;
  primary_metric: Record<string, string>;
  labels: Record<SiLabel, string>; label_rule: string; notes: string[];
};

// ------------------------------------------------------------------ playoff race lab --

export type PrRecord = { wins: number; losses: number; ties: number; unresolved?: number };
export type PrStatus = "inside" | "outside" | "cutline_tie" | null;
export type PrStanding = { team_id: number; record: PrRecord | null; pct: number | null; rank: number | null; tied: boolean; status: PrStatus };

export type PrSide = {
  projected_points: number | null; known_points: number | null; complete: boolean;
  weekly: { week: number; projected_lineup_points: number | null; known_lineup_points: number | null; projection_complete: boolean }[];
  missing_projection_ids: number[]; missing_players: string[]; gap: string | null;
};

export type PrOutcome = "home" | "away" | "tie";
export type PrMatchup = {
  matchup_id: number; matchup_period: number; weeks: number[] | null;
  home_team_id: number | null; away_team_id: number | null; home_name: string | null; away_name: string | null;
  espn_winner: string | null; playoff_tier: string | null;
  status: "final" | "bye" | "in_progress" | "upcoming" | "unmapped"; detail?: string;
  result?: PrOutcome; home_points?: number | null; away_points?: number | null;
  home_projection?: PrSide; away_projection?: PrSide; projected_margin?: number | null; close?: boolean | null;
  weekly_margins?: { week: number; margin: number }[]; projected_winner?: PrOutcome | null;
  override?: PrOutcome | "none"; scenario_result: PrOutcome | null;
};

export type PrRemaining = {
  matchup_id: number; matchup_period: number; weeks: number[] | null; opponent_id: number | null; opponent_name: string | null;
  home: boolean; status: PrMatchup["status"]; projected_margin: number | null; close: boolean | null; scenario_result: "win" | "loss" | "tie" | null;
};

export type PrTeam = {
  team_id: number; name: string; abbrev: string | null; is_mine: boolean; division_id: number | null; espn_seed: number | null;
  record: PrRecord; points_for: number | null; points_against: number | null;
  current: PrStanding; projected: PrStanding;
  all_play: { wins: number; losses: number; ties: number; periods: number[]; pct: number | null };
  regular_season_projected_points: number | null; regular_season_known_points: number | null;
  projection_weeks_incomplete: number[] | null; projection_gap: string | null;
  remaining: PrRemaining[]; high_leverage_games: number[];
};

export type PrLeverage = {
  matchup_id: number; matchup_period: number; weeks: number[] | null; opponent_id: number | null; status: string;
  close: boolean | null; scenario_result: "win" | "loss" | "tie" | null;
  if_win: { rank: number | null; status: PrStatus }; if_loss: { rank: number | null; status: PrStatus }; high_leverage: boolean;
};

export type PrPlace = { rank: number | null; status: PrStatus; record: PrRecord | null };

export type PlayoffRace = {
  league: { league_id: number; season: number; name: string; current_week: number; current_matchup_period: number | null;
    final_scoring_period: number; reg_season_matchups: number | null; playoff_teams: number | null;
    last_regular_season_week: number | null; divisions: number; matchup_periods: Record<string, number[] | null> };
  window: { first_actionable_week: number | null; reason: string | null; uncertain: boolean | null; current_week_status: string | null };
  focus_team_id: number | null;
  teams: PrTeam[];
  matchups: { regular: PrMatchup[]; playoff_known: PrMatchup[] };
  scenario: {
    base: "projected" | "actual"; overrides: { matchup_id: number; outcome: PrOutcome | "none" }[];
    decided_open_games: number; unresolved_open_games: number;
    current_standings: PrStanding[]; projected_standings: PrStanding[];
    // Boundary groups are whole rank groups; a group straddling the last spot is cutline_tie (no tiebreak).
    cutline: { playoff_spots: number; last_inside: PrStanding[]; first_outside: PrStanding[];
      cutline_tie: { team_ids: number[]; spots_left: number } | null; bubble: number[] } | null;
    leverage: Record<string, PrLeverage[]>;
    focus: { scenario: PrPlace; win_out: PrPlace; lose_out: PrPlace; open_games: number;
      fewest_wins_to_get_inside: { wins_needed: number | null; of_games: number; method: string; games: number[]; note?: string } | null;
      helpful_other_results: { matchup_id: number; matchup_period: number; result: PrOutcome; loser_id: number; rank: number | null; status: PrStatus }[] } | null;
  };
  schedule_coverage: { checked: boolean; reason?: string; regular_season_periods?: number; complete?: boolean;
    missing: { team_id: number; periods: number[] }[]; unmapped_periods?: number[] };
  record_check: string[];
  rules: { ordering: string; projection: string; close: string; clinch: string; espn_rules_reported: Record<string, unknown>; espn_rules_note: string };
  warnings: string[]; notes: string[];
  /** Present only when at least one result is set (issue #192); absent, the answer is the scenario-off answer. */
  whatif?: PrWhatIf;
};

export type PrPlaceRow = { rank: number | null; tied: boolean; status: PrStatus; record: PrRecord | null };
export type PrSetResult = {
  matchup_id: number; matchup_period: number; weeks: number[] | null; home_team_id: number; away_team_id: number;
  status: PrMatchup["status"]; outcome: PrOutcome | "none"; baseline_result: PrOutcome | null; changes_baseline: boolean;
};
export type PrBlocker = { rule: string; state: string; reported: Record<string, unknown>; detail: string };
export type PrBound = {
  team_id: number; open_games: number; best: PrRecord | null; worst: PrRecord | null;
  teams_able_to_match_worst: number | null; teams_certain_ahead_of_best: number | null;
  record_order: "above_line" | "below_line" | "open" | null; claim: null;
};
export type PrWhatIf = {
  label: "hypothetical"; method: string;
  sampling: { method: "deterministic"; scenarios: number; random_samples: number; seed: null; probabilities: null };
  inputs: { base: "projected" | "actual"; first_actionable_week: number | null; set_results: PrSetResult[] };
  assumptions: string[];
  completeness: { status: "complete" | "partial"; unresolved_games: number; by_reason: Record<string, number> };
  labels: Record<string, string>;
  baseline_standings: PrStanding[];
  changes: { team_id: number; baseline: PrPlaceRow; whatif: PrPlaceRow; rank_change: number | null; status_changed: boolean }[];
  conclusions: { status: "blocked" | "open"; blocked_by: PrBlocker[]; claims: never[];
    record_bounds: { rule: string; fixed_results: number; open_games: number; abstain: string[]; teams: PrBound[] } };
};

// ------------------------------------------------------------------- my team value --

export type TvPhase = "all" | "regular" | "playoff";
export type TvRankRow = { team_id: number; name: string | null; total: number | null; average_weekly: number | null; rank: number };
export type TvPhaseSplit = {
  weeks: number[]; projected_points: number | null; known_points: number | null; average_weekly: number | null;
  incomplete_weeks: number[]; league_rank: TvRankRow | null;
};
export type TvRaw = { points: number | null; known_points: number | null; missing_weeks: number[] };
/** code: bye | zero_projection | no_projection | ir | taxi (Sleeper taxi squad) | streamable | flex_competition | outscored. */
export type TvBench = { week: number; projection: number | null; code: string; text: string };
export type TvPlayer = {
  espn_player_id: number; name: string | null; position: string | null; pro_team: string | null;
  injury_status: string | null; on_ir: boolean; on_taxi?: boolean;
  espn_projected: Record<TvPhase, TvRaw>;
  marginal: { all: number | null; regular: number | null; playoff: number | null; known_all: number | null;
    known_regular: number | null; known_playoff: number | null };
  weeks_started: number[]; weeks_benched: number[]; weeks_expected: number; projection_complete: boolean;
  weekly: { week: number; phase: string | null; marginal: number | null; known_marginal: number; started: boolean }[];
  bench: TvBench[]; bench_reason_counts: Record<string, number>; unused_projected_points: number; started_points: number;
  ir_activation_sensitivity: { lineup_points_upside: number | null; known_lineup_points_upside: number; weeks_would_start: number[];
    roster_move_note: string | null; assumption: string } | null;
};
export type TvRemoval = {
  espn_player_id: number; name: string | null; weeks_in_slot: number[]; lineup_points_lost: number | null;
  lineup_points_lost_by_phase: Record<TvPhase, number | null>;
  known_lineup_points_lost: number; known_lineup_points_lost_by_phase: Record<TvPhase, number | null>; own_started_points: number; recovered_by_roster: number | null;
  fill_ins: { espn_player_id: number; name: string | null; weeks: number[] }[];
};
export type TvSlot = {
  slot: string; count: number;
  strength: Record<TvPhase, { average: number | null; rank: number | null; teams_ranked: number; league_best: number | null }>;
  removal_scenarios: TvRemoval[]; largest_single_loss: TvRemoval | null;
};
export type TvMatchupEffect = {
  matchup_id: number; matchup_period: number; weeks: number[]; opponent_id: number; opponent_name: string | null;
  margin_before: number | null; margin_after: number | null; projected_result_before: string | null;
  projected_result_after: string | null; flips: boolean; changes_projected_result: boolean;
};
export type TvLite = { espn_player_id: number; name: string | null; position: string | null; pro_team?: string | null };
export type TeamValue = {
  team: { team_id: number; name: string; abbrev: string | null; is_mine: boolean };
  teams: { team_id: number; name: string; is_mine: boolean }[];
  league: { name: string; season: number; current_week: number; playoff_teams: number | null; last_regular_season_week: number | null };
  window: { weeks: number[]; first_actionable_week: number | null; last_regular_season_week: number | null; final_week: number;
    reason: string | null; uncertain: boolean; warning: string | null };
  freshness: { league_synced_at: string | null; projections_synced_at: string | null };
  standing: { record: PrRecord; espn_seed: number | null; points_for: number | null; points_against: number | null;
    current: PrStanding; projected: PrStanding; playoff_spots: number | null };
  notes: string[];
  empty?: boolean; message?: string;
  overview?: {
    summary: { ros_projected_starting_points: number | null; ros_known_starting_points: number; missing_weeks: number[];
      missing_players: number[]; ros_weeks_expected: number; ros_weeks_complete: number };
    phases: Record<TvPhase, TvPhaseSplit>; missing_players: string[];
    weekly: { week: number; phase: string | null; projected_lineup_points: number | null; known_lineup_points: number;
      projection_complete: boolean; league_median: number | null }[];
    league_ranks: { rule: string; teams_ranked: number[]; teams_unranked: number[];
      excluded_weeks: { week: number; incomplete_teams: string[]; reason?: string }[];
      phases: Record<TvPhase, { weeks: number[]; phase_weeks: number[]; teams: TvRankRow[] }> };
  };
  players?: TvPlayer[];
  positions?: { slots: TvSlot[]; strongest: string[]; weakest: string[];
    bench_coverage: { espn_player_id: number; name: string | null; covers: { for: string | null; slot: string; weeks: number[] }[] }[]; note: string };
  bench?: { players: { espn_player_id: number; name: string | null; position: string | null; unused_projected_points: number;
      started_points: number; weeks_started: number[]; weeks_benched: number[]; bench_reason_counts: Record<string, number>;
      marginal: number | null; known_marginal: number; espn_projected: TvRaw }[];
    consolidation_candidates: number[]; ir_players: number[]; note: string };
  upgrades?: {
    slot_gaps: { week: number; phase: string | null; slot: string; current_player_id: number | null; current_name: string | null; current_is_streamer?: boolean;
      current_points: number | null; best_available: TvLite & { points: number }; gap: number }[];
    free_addition: { player: TvLite | null; lineup_points_added: number | null; known_lineup_points_added: number | null;
      regular_season_points_added: number | null; playoff_points_added: number | null; weeks_started: number[] | null;
      open_active_roster_spots: number; realistic: boolean; label: string; candidates: number; excluded_unprojected: number;
      excluded_position_limit: number };
    add_drop: { add: TvLite; drop: TvLite; ros_delta: number | null; regular_season_delta: number | null; playoff_delta: number | null;
      known_ros_delta: number; drop_marginal: number | null }[];
    waiver_checks: { add_id: number; add_name: string | null; position: string | null; recommendation: string; waiver_score: number | null;
      drop_id: number | null; drop_name: string | null; status: string; ros_delta?: number | null; known_ros_delta?: number; regular_season_delta?: number | null;
      playoff_delta?: number | null }[];
    trade_preview: { list: string; opponent_team_id: number; opponent_name: string; sends: number[]; receives: number[];
      send_names: (string | null)[]; receive_names: (string | null)[];
      mine: { ros_delta: number | null; regular_season_delta: number | null; playoff_delta: number | null };
      theirs: { ros_delta: number | null; regular_season_delta: number | null; playoff_delta: number | null };
      forced_drops: { mine: number[]; theirs: number[] }; categories: string[]; consistent_with_manual_evaluator: boolean;
      evaluator_link: { ta: number; tb: number; a: number[]; b: number[] } }[];
    trade_note: string | null; trade_preview_rule: string; waiver_rule: string;
  };
  playoff_race?: {
    standing_now: PrStanding; standing_projected: PrStanding; cutline: PlayoffRace["scenario"]["cutline"];
    remaining: PrRemaining[]; close_games: PrRemaining[]; leverage: PrLeverage[];
    upgrade_effects: { add_drop: { add: TvLite; drop: TvLite; matchups: TvMatchupEffect[] } | null;
      trade: { opponent_team_id: number; sends: number[]; receives: number[]; matchups: TvMatchupEffect[] } | null };
    rule: string;
  };
};
