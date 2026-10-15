// V3 trade analyzer + discovery API types (GET /api/trades/options, POST /api/trades/evaluate,
// GET /api/trades/discover). Kept apart from lib/types.ts.
import type { League, ManagerContext, TradeDeltas, TradeResult, TradeSide, TradeWindow } from "@/lib/types";

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

export type TradeResultV3 = Omit<TradeResult, "team_a" | "team_b" | "players" | "trade"> & {
  trade: TradeResult["trade"] & { team_a_sends_count: number; team_b_sends_count: number; max_players_per_side: number };
  team_a: TradeSideV3; team_b: TradeSideV3;
  players: (TradeResult["players"][number] & { availability: AvailabilityRecord | null })[];
  injury_warnings: { espn_player_id: number; name: string | null; status: string; warnings: string[] }[];
  injury_scenarios: InjuryScenarios[];
  availability_note: string;
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
    shapes: string[]; sort: string; category: string | null; opponent: number | null; limit: number; heuristic: boolean };
  mode_text: string;
  sections: Record<string, DResult[]>;
  tiers: Record<string, string>;
  excluded: { one_sided: number; lopsided: number };
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
  kdst_policy: string; availability_note: string; note: string; assumptions: string[]; limitations: string[];
};
