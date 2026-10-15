export interface LineupPlayer {
  slot_id: string;
  slot: string;
  player_id: number | null;
  name: string | null;
  position: string | null;
  pro_team: string | null;
  projected_points: number | null;
  is_flex: boolean;
  is_currently_starting: boolean;
  current_slot: string | null;
  lock_status: "LOCKED" | "UNLOCKED" | "UNKNOWN" | "BYE" | "TEAMLESS";
  kickoff_utc: string | null;
  has_final_score?: boolean;
  home_score?: number | null;
  away_score?: number | null;
  opponent: string | null;
  is_home?: boolean | null;
  injury_status?: string | null;
  availability_status?: string | null;
}

export interface DecisionPlayer {
  id: number;
  name: string;
  position: string;
  pro_team: string;
  projection: number | null;
  lock_status: string;
}

export interface StartSitDecision {
  id: string;
  action: "SWAP" | "COORDINATED_SWAP" | "NO_CHANGE";
  actionability: "ACTIONABLE" | "LOCKED" | "UNCERTAIN" | "IR_BLOCKED" | "NO_CHANGE";
  start_players: DecisionPlayer[];
  bench_players: DecisionPlayer[];
  target_slot: string | null;
  projected_net_gain: number | null;
  explanation: string;
  warnings: string[];
}

export interface BenchPlayer {
  player_id: number;
  name: string;
  position: string;
  pro_team: string | null;
  projected_points: number | null;
  lock_status: "LOCKED" | "UNLOCKED" | "UNKNOWN" | "BYE" | "TEAMLESS";
  kickoff_utc: string | null;
  opponent: string | null;
  is_home?: boolean | null;
  injury_status?: string | null;
  on_ir: boolean;
  nfl_ineligible: boolean;
}

export interface StartSitSummary {
  actual_projected_total: number | null;
  recommended_projected_total: number | null;
  known_actual_total: number;
  known_recommended_total: number;
  unconstrained_total: number | null;
  known_unconstrained_total: number;
  projected_gain: number | null;
  projection_complete: boolean;
  missing_projections: Array<{ player_id: number; name: string; position: string }>;
  starters_count: number;
  bench_count: number;
  ir_count: number;
  locked_players_count: number;
  status: "optimal" | "upgrade_available" | "incomplete_data" | "all_locked" | "future_preview";
}

export interface InjuryWarning {
  player_id: number;
  name: string;
  position: string;
  injury_status?: string | null;
  on_ir: boolean;
  nfl_ineligible: boolean;
  warnings: string[];
}

export interface StartSitResponse {
  league: {
    id: number;
    name: string;
    season: number;
    current_week: number;
    final_scoring_period: number;
  };
  team: {
    id: number;
    name: string;
    abbrev: string;
    is_mine: boolean;
  };
  teams?: Array<{
    id: number;
    name: string;
    abbrev: string;
    is_mine: boolean;
  }>;
  scoring_period: number;
  is_future_preview: boolean;
  projection_source: string;
  summary: StartSitSummary;
  actual_lineup: LineupPlayer[];
  recommended_lineup: LineupPlayer[];
  unconstrained_lineup: LineupPlayer[];
  decisions: StartSitDecision[];
  current_bench: BenchPlayer[];
  injury_warnings: InjuryWarning[];
  schedule_warnings: string[];
  freshness: {
    lineup_synced_at: string | null;
    projection_synced_at: string | null;
    nfl_schedule_synced_at: string | null;
  };
  limitations: string[];
}
