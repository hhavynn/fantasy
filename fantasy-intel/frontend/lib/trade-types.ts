// V3 trade analyzer + discovery API types (GET /api/trades/options, POST /api/trades/evaluate,
// GET /api/trades/discover). Kept apart from lib/types.ts.
import type { ProvenancePayload, WithProvenance } from "./signal-provenance";
import type { KdstStreaming, League, ManagerContext, TradeDeltas, TradeResult, TradeSide, TradeWindow } from "@/lib/types";

export type AvailabilityBrief = {
  status: string; nfl_designation_label: string | null; earliest_eligible_week: number | null;
  return_confirmed: boolean; fantasy_ir_slot: boolean; espn_injury_status: string | null; warnings: string[];
};

export type OptionPlayer = {
  espn_player_id: number; name: string | null; position: string | null; pro_team: string | null;
  on_ir: boolean; injury_status: string | null; ros_projected_points: number | null;
  nfl_ineligible_weeks: number[]; availability: AvailabilityBrief | null;
};
export type OptionTeam = { team_id: number; name: string; abbrev: string; is_mine: boolean; roster: OptionPlayer[] };
export type TradeOptionsV3 = {
  league: League; evaluation: TradeWindow; teams: OptionTeam[]; active_capacity: number;
  position_limits: Record<string, number>; max_players_per_side: number; availability_note: string;
  /** signal-provenance.v1: league data, rest-of-season projections, injury reports (lib/signal-provenance.ts). */
  provenance?: ProvenancePayload | null;
};

export type AvailabilityRecord = AvailabilityBrief & {
  nfl_designation: string | null; nfl_designation_source: string | null; designation_since_week: number | null;
  eligibility_basis: string | null; expected_return_week: number | null; expected_return_source: string;
  unavailable_weeks: number[]; ignored_projection: { week: number; espn_projection: number | null }[];
  espn_status_synced_at: string | null; nfl_latest_roster_week: number | null;
};

export type InjuryScenario = {
  scenario: "earliest_eligible_return" | "as_published" | "delayed_to_playoffs" | "no_return"; return_week: number | null;
  description: string; canonical: boolean; error?: string;
  team_a?: { ros_delta: number | null; regular_season_delta: number | null; playoff_delta: number | null; known_ros_delta: number };
  team_b?: { ros_delta: number | null; regular_season_delta: number | null; playoff_delta: number | null; known_ros_delta: number };
};
export type InjuryScenarios = {
  espn_player_id: number; name: string | null; position: string | null; status: string; earliest_eligible_week: number | null;
  eligibility_basis: string | null; return_confirmed: boolean; scenarios: InjuryScenario[]; note: string;
};

export type RosterIssues = {
  active_before_drops: number; active_capacity: number; over_capacity_by: number;
  position_limits: { position: string; limit: number; count_before_drops: number; over_by: number }[];
};

export type TradeSideV3 = TradeSide & {
  roster: TradeSide["roster"] & { issues_before_drops: RosterIssues;
    forced_drop_search: { drops: number; candidate_sets: number; lineup_solves: number; method: string } | null };
  outgoing_usage: { espn_player_id: number; name: string | null; position: string | null; weeks_started: number[]; slots: Record<string, number> }[];
  displaced: { espn_player_id: number; name: string | null; position: string | null; starts_lost: number[]; starts_gained: number[] }[];
};

export type TradeResultV3 = Omit<TradeResult, "team_a" | "team_b" | "players" | "trade"> & WithProvenance & {
  trade: TradeResult["trade"] & { team_a_sends_count: number; team_b_sends_count: number; max_players_per_side: number };
  team_a: TradeSideV3; team_b: TradeSideV3;
  players: (TradeResult["players"][number] & { availability: AvailabilityRecord | null })[];
  injury_warnings: { espn_player_id: number; name: string | null; status: string; warnings: string[] }[];
  injury_scenarios: InjuryScenarios[];
  availability_note: string;
  trade_vs_free_agency?: TradeVsFreeAgency;
};

// ------------------------------------------------------------------------- discovery --

export type AssetProfile = {
  espn_player_id: number; name: string | null; position: string | null; par: number | null; known_par: number;
  par_regular: number | null; par_playoff: number | null; espn_points_eligible_weeks: number | null;
  replacement_per_week: number | null; replacement_source: string[]; missing_weeks: number[];
  nfl_ineligible_weeks: number[]; availability_status: string | null; fantasy_ir_slot: boolean; return_unconfirmed: boolean;
  eligibility_unknown: boolean; position_rank: number | null; position_rank_of: number | null;
  league_starters_at_position: number | null; starter_caliber: boolean;
};
export type AssetBalance = {
  label: string; text: string; note: string; you_send: AssetProfile[]; they_send: AssetProfile[];
  you_send_par: number | null; they_send_par: number | null; ratio: number | null; caveats: string[];
  per_week?: { you_send: number; they_send: number };
  thresholds: { balanced_ratio: number; significant_ratio: number; replacement_per_week: number };
};

// ------------------------------------------------------------------ dual value (Trade Discovery 2.0) --
// analytics/trade_dual_value.py, docs/trade_discovery_dual_value.md. Every field below is additive: an older backend
// (or a legacy mode) may omit it, so the page renders without it. Nothing here is an acceptance estimate.

export type CheckStatus = "pass" | "concern" | "fail" | "unknown" | "not_applicable";
export type Criterion = "lineup_gain" | "asset_balance" | "positional_fit" | "depth_cost" | "free_agent_alternative" | "uncertainty";
export type DVCheckBrief = { criterion: string; status: CheckStatus };
export type DVCheck = DVCheckBrief & { text?: string };

export type LineupPoints = { ros: number | null; regular: number | null; playoff: number | null };
export type DVSideSummary = {
  before: LineupPoints; after: LineupPoints;
  delta: LineupPoints & { known_ros: number | null };
  average_weekly_delta: number | null;
};
export type DVAssetSummary = {
  label: string; text: string; ratio: number | null; imbalance: "none" | "moderate" | "significant" | null;
  favours: string | null; you_send_par: number | null; they_send_par: number | null;
};
/** A summary row's `dual_value_summary` (`GET /discover/summary`). */
export type DualValueSummary = {
  verdict: string; verdict_text: string; main_concern: string | null; front: number | null;
  you_change: string | null; them_change: string | null;
  asset: DVAssetSummary;
  lineup: { you: DVSideSummary; them: DVSideSummary };
  checks: DVCheckBrief[];
};

export type DVPlayerAsset = {
  espn_player_id: number; name: string | null; position: string | null; par: number | null; known_par: number | null;
  par_regular: number | null; par_playoff: number | null; position_rank: number | null; position_rank_of: number | null;
  league_starters_at_position: number | null; starter_caliber: boolean | null; missing_weeks: number[];
  nfl_ineligible_weeks: number[]; replacement_per_week: number | null;
  from_side: string; to_side: string; starts_before: number | null; starts_after: number | null; weeks: number;
  role_before: string | null; role_after: string | null; bench_asset: boolean;
};
export type DVBestAsset = { espn_player_id: number; name: string | null; position: string | null; par: number | null;
  position_rank: number | null; starter_caliber: boolean | null };
export type DVDepth = {
  net_players: number; kind: "consolidates" | "adds depth" | "even";
  forced_drops: { espn_player_id: number; name: string | null; position: string | null; par: number | null; starter_caliber: boolean }[];
  forced_drop_par: number | null; forced_drop_lineup_cost: number | null; outgoing_non_starter_par: number | null;
  open_roster_spots_after: number | null; empty_slots_after: { week: number; slot: string }[];
};
export type DVAsset = DVAssetSummary & {
  per_week?: { you_send: number; they_send: number } | null; caveats: string[]; note: string;
  you_send: DVPlayerAsset[]; you_receive: DVPlayerAsset[];
  best_asset_surrendered: { you: DVBestAsset | null; them: DVBestAsset | null };
  bench_assets: number[]; depth: { you: DVDepth; them: DVDepth };
};

export type DVWeekly = { week: number; phase: string | null; delta: number | null; known_delta?: number | null;
  entered: number[]; exited: number[] };
export type DVPlayerChange = { espn_player_id: number; name: string | null; position: string | null; weeks: number[]; incoming?: boolean };
export type DVLineupSide = DVSideSummary & {
  label: string; team_id: number; team_name: string | null;
  weeks_improved: number; weeks_worsened: number; weeks_unchanged: number;
  promoted: DVPlayerChange[]; displaced: DVPlayerChange[];
  forced_drops: { espn_player_id: number; name: string | null; position: string | null }[];
  forced_drop_lineup_cost: number | null;
  missing: { weeks: number[]; players: number[]; weeks_incomplete: number };
  weekly: DVWeekly[]; key_change: string | null;
};
export type DualValue = {
  phase: string;
  asset: DVAsset;
  lineup: { you: DVLineupSide; them: DVLineupSide; weeks: number; basis: string };
  plausibility: { verdict: string; verdict_text: string; checks: DVCheck[]; main_concern: string | null; criteria?: Record<string, string> };
  rank_axes?: { lineup_min_side: number | null; asset_ratio: number | null; certainty: number | null };
  rank?: { front: number | null; policy: string };
  most_important_change?: string | null;
  projection_source?: string;
};

/** 0-100 ratings (`trade_ratings.evidence`): supporting context only, never summed or compared across positions. */
export type DVRating = {
  position: string | null; opportunity_basis: string | null; games: number | null; rating_scale_version: string | null;
  opportunity_score_100: number | null; production_score_100: number | null; value_score_100: number | null;
};
export type DRatings = {
  available: boolean; reason: string | null; version: string | null; note?: string;
  players: Record<string, DVRating | null>;
  comparable_pairs: { you_send: number; you_receive: number; position: string | null }[];
};
export type DName = { name: string | null; position?: string | null; pro_team?: string | null };

/** Top-level fields the dual-value backend adds to the context and summary payloads (all optional on an older backend). */
export type DualValueTop = {
  lens?: { mode: string; label: string; pool_rule: string } | null;
  lens_note?: string | null;
  ranking_policy?: string;
  verdicts?: Record<string, string>;
  criteria?: Record<string, string>;
  projection_source?: { used: string; experimental_comparison: unknown; note: string };
};

export type DUsage = { espn_player_id: number; name: string | null; position: string | null; weeks_started: number[]; slots: Record<string, number> };
export type DSide = {
  team_id: number; team_name: string | null; deltas: TradeDeltas;
  before: Record<string, number | null>; after: Record<string, number | null>;
  incoming_usage: DUsage[]; outgoing_usage: DUsage[];
  displaced: { espn_player_id: number; name: string | null; position: string | null; starts_lost: number[]; starts_gained: number[] }[];
  fit_notes: string[];
  forced_drops: { espn_player_id: number; name: string | null; position: string | null; is_incoming: boolean }[];
  forced_drop_lineup_cost: number | null;
  empty_slots_after: { week: number; slot: string }[];
  incomplete: { missing_weeks: number[]; missing_players: number[] };
};

export type DRow = {
  key: string; shape: string; search_method: "exact" | "heuristic_beam"; anchor_team_id: number; opponent_team_id: number;
  opponent_name: string | null; sends: number[]; receives: number[]; mine: TradeDeltas; theirs: TradeDeltas;
  combined_ros_delta: number | null; min_side_ros_delta: number | null; forced_drops: { mine: number[]; theirs: number[] };
  categories: string[];
};
export type DResult = DRow & {
  consistent_with_manual_evaluator: boolean; result_label: string;
  mine_detail: DSide; theirs_detail: DSide;
  asset_balance: AssetBalance; tier: string; tier_reasons: string[];
  injury_flags: { espn_player_id: number; status: string; warnings: string[]; earliest_eligible_week: number | null; return_confirmed: boolean }[];
  injury_scenarios: InjuryScenarios[];
  rationale: { trade_type: { kind: string; text: string }; points: string[]; concerns: string[] };
  manager_context: ManagerContext | null; need_notes: string[];
  trade_vs_free_agency?: TradeVsFreeAgency;
  dual_value?: DualValue; ratings?: DRatings;
};

export type NeedRow = { slot_points_per_week: number | null; slots: number; surplus: number[]; league_rank: number | null;
  teams: number; short: boolean; incomplete: boolean };

export type DiscoveryV3 = {
  league: { league_id: number; season: number; name: string; current_week: number; final_scoring_period: number };
  anchor: { team_id: number; name: string; abbrev: string; is_mine: boolean };
  teams: { team_id: number; name: string; abbrev: string; is_mine: boolean }[];
  evaluation: { start_week: number; final_week: number; last_regular_season_week: number | null; weeks: number[];
    uncertain: boolean; warning: string | null; start_week_reason: string | null };
  query: { team_id: number; mode: string; phase: string; strictness: string; positions: string[]; include_kdst: boolean;
    shapes: string[]; sort: string; category: string | null; opponent: number | null; limit: number; heuristic: boolean;
    kdst_streaming?: boolean };
  mode_text: string;
  sections: Record<string, DResult[]>;
  tiers: Record<string, string>;
  excluded: Record<string, number | undefined>;
  pool_rule: string; pool_size: number;
  coverage: {
    exact: { candidates_considered: number; screened_out_by_upper_bound: number; fully_evaluated: number; matching_filters: number;
      excluded_invalid_roster: number; excluded_missing_data: number; not_evaluated: number; not_evaluated_reason: string | null;
      termination: string; exhaustive: boolean; bounds_valid: boolean; seconds: { stage_a: number; stage_b: number } } | null;
    heuristic: { method: string; shapes: string[]; exhaustive: boolean; packages_evaluated: number; qualifying_rows: number;
      beam: { seeds: number; width: number; pool_per_signal: number }; seconds: number; note: string } | null;
    exact_shapes: string[]; heuristic_shapes: string[]; positions: string[] | null;
  };
  opponents: { opponent_team_id: number; team_name: string | null;
    best: (Pick<DRow, "key" | "shape" | "sends" | "receives" | "mine" | "theirs" | "search_method"> & { tier: string; asset_label: string }) | null;
    needs: Record<string, NeedRow> }[];
  my_needs: Record<string, NeedRow>;
  asset_note: string; asset_thresholds: { balanced_ratio: number; significant_ratio: number; replacement_per_week: number };
  search: { consistency_checked: number; consistency_failures: number };
  names: Record<string, { name: string | null; position: string | null; pro_team: string | null; on_ir: boolean;
    injury_status: string | null; nfl_ineligible_weeks: number[] }>;
  modes: Record<string, string>; phases: string[]; strictness_options: Record<string, string>;
  categories: Record<string, string>; sorts: string[]; exact_shapes: string[]; heuristic_shapes: string[];
  kdst_policy: string; kdst_streaming?: KdstStreaming; availability_note: string; note: string; assumptions: string[]; limitations: string[];
} & DualValueTop;

/** The page header and filters (`GET /api/trades/discover/context`): no search is run. */
export type DiscoveryContext = Pick<DiscoveryV3, "league" | "anchor" | "teams" | "evaluation" | "query" | "mode_text" | "modes"
  | "phases" | "strictness_options" | "categories" | "sorts" | "exact_shapes" | "heuristic_shapes"> & DualValueTop & WithProvenance;

/** One results-table row (`GET /api/trades/discover/summary`); the rest of the row is its `DEvidence`. */
export type DSummaryRow = Pick<DRow, "key" | "shape" | "search_method" | "anchor_team_id" | "opponent_team_id" | "opponent_name"
  | "sends" | "receives" | "mine" | "theirs" | "combined_ros_delta" | "min_side_ros_delta" | "categories"> & {
  tier: string; consistent_with_manual_evaluator: boolean;
  asset_balance: Pick<AssetBalance, "label" | "text" | "note" | "ratio" | "you_send_par" | "they_send_par">;
  rationale: { trade_type: { kind: string; text: string }; concerns: string[] };
  manager_context: { trade_history: Pick<ManagerContext["trade_history"], "completed_trades" | "confidence"> } | null;
  dual_value_summary?: DualValueSummary | null;
};

/** `/discover` with each row cut to its table summary; `names` covers only the players shown. */
export type DiscoverySummary = Omit<DiscoveryV3, "sections"> & { sections: Record<string, DSummaryRow[]> };

/** One row's evidence (`GET /api/trades/discover/evidence`, proxied by `/api/trade-evidence`). */
export type DEvidence = Pick<DResult, "key" | "search_method" | "opponent_name" | "anchor_team_id" | "opponent_team_id" | "sends"
  | "receives" | "trade_vs_free_agency" | "injury_scenarios" | "manager_context" | "need_notes"> & {
  rationale: { points: string[]; concerns: string[] };
  asset_balance: Pick<AssetBalance, "you_send" | "they_send" | "caveats">;
  mine_detail: Pick<DSide, "team_id" | "team_name" | "fit_notes">;
  theirs_detail: Pick<DSide, "team_id" | "team_name" | "fit_notes">;
  /** Dual-value assessment, 0-100 ratings and the names of the players the weekly lineup changes mention. */
  dual_value?: DualValue | null; ratings?: DRatings | null; names?: Record<string, DName>;
};

// ------------------------------------------------------- trade vs. free agency (V3.1) --
// analytics/trade_alternatives.py: one hypothetical acquisition next to the canonical delta (never in it).

export type TvfDeltas = {
  ros_delta: number | null; regular_season_delta: number | null; playoff_delta: number | null;
  known_ros_delta: number | null; projection_complete: boolean;
};
export type TvfPlayer = { espn_player_id: number; name: string | null; position: string | null; pro_team: string | null };
export type TvfAcquisition = {
  player: (TvfPlayer & { acquisition_type: string }) | null; reason: string | null; acquisition_type: string | null;
  acquisition_note?: string; drops: TvfPlayer[]; requires_drop: boolean; drop_lineup_cost: number | null;
  drop_cost_unknown: boolean; gain: TvfDeltas; weeks_started: number[];
  search: { candidates: number; after_dominance: number; solved: number; excluded_unprojected: number; method: string };
};
export type TvfFrontierWeek = {
  week: number; phase: string | null; starter_points: number | null; starter_slots?: (number | null)[];
  gain: number | null; known_gain: number;
  best_available: (TvfPlayer & { points: number; acquisition_type: string })[];
};
export type TvfFrontier = {
  position: string; slot_count?: number; label: string; total_gain: number | null; known_total_gain: number;
  weeks: TvfFrontierWeek[];
};
export type TvfSlotRow = { slot: string } & Record<string, number | string>;
export type TvfSide = {
  team_id: number; team_name: string | null; label: string;
  comparison: {
    canonical_gain: TvfDeltas; free_agent_alternative_gain: TvfDeltas; trade_advantage: TvfDeltas;
    available_without_trade: TvfDeltas; trade_instead_of_free_agent: TvfDeltas;
    trade_plus_best_free_agent_gain: TvfDeltas; trade_advantage_if_both_add: TvfDeltas;
    share_available_without_trade: number | null;
    interaction_type?: string;
    interaction_note?: string | null;
  };
  free_agent_alternative: TvfAcquisition;
  trade_with_free_agent: {
    available?: boolean; reason?: string | null; drops: TvfPlayer[]; drop_cost_unknown: boolean;
    retained_player_ids?: number[]; error?: string;
  } | null;
  waiver_claim_alternative: TvfAcquisition | null;
  unknown_availability_alternative?: TvfAcquisition | null;
  position_alternatives: (TvfAcquisition & { position: string; slot_delta: number | null; same_as_headline?: boolean })[];
  with_trade_alternative: TvfAcquisition;
  slot_breakdown: TvfSlotRow[];
  streaming_frontier: { position: string; no_trade: TvfFrontier; after_trade: TvfFrontier }[];
  statements: string[]; projection_complete: boolean;
};
export type TradeVsFreeAgency =
  | { available: false; reason: string }
  | {
    available: true; consistent_with_canonical: boolean; label: string; note: string; majority_rule: string;
    formulas: Record<string, string>; team_a: TvfSide; team_b: TvfSide;
    contested: { player: TvfPlayer & { acquisition_type: string }; note: string } | null;
    pool: { available_players_scored: number; excluded_unprojected: Record<string, number>; acquisition_types: Record<string, number> };
    assumptions: string[]; limitations: string[];
  };
