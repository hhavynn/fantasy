import type { ProjectionOrigin, ProjectionSourceDetail } from "@/lib/projection-source";
import type { LineupCoverage } from "@/lib/lineup-coverage";
import type { ProvenancePayload } from "@/lib/signal-provenance";

export interface LineupPlayer {
  slot_id: string;
  slot: string;
  player_id: number | null;
  name: string | null;
  position: string | null;
  pro_team: string | null;
  projected_points: number | null;
  projection_origin?: ProjectionOrigin | null;
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
  projection_origin?: ProjectionOrigin | null;
  lock_status: string;
}

/** How close a recommendation is (issue #172): one close rule (a gain of at most 3 projected points, the backtests'
 *  close-call margin), unverified on any unknown. A label with its reason, never a probability. */
export interface RecommendationStrength {
  label: "clear" | "close" | "unverified";
  reason: string;
}

/** A legal single move into one recommended starting slot (issue #172): the projected total's change against the
 *  recommendation (null when unknown) and the what-if scenario that tries it. */
export interface AlternativeOption {
  player_id: number;
  name: string;
  position: string | null;
  projection: number | null;
  delta: number | null;
  known_delta: number;
  whatif: string;
}

export interface SlotAlternatives {
  slot_id: string;
  slot: string;
  label: string;
  starter_id: number | null;
  options: AlternativeOption[];
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
  /** Absent from an older backend: then nothing is graded. */
  recommendation_strength?: RecommendationStrength | null;
}

export interface BenchPlayer {
  player_id: number;
  name: string;
  position: string;
  pro_team: string | null;
  projected_points: number | null;
  projection_origin?: ProjectionOrigin | null;
  lock_status: "LOCKED" | "UNLOCKED" | "UNKNOWN" | "BYE" | "TEAMLESS";
  kickoff_utc: string | null;
  opponent: string | null;
  is_home?: boolean | null;
  injury_status?: string | null;
  on_ir: boolean;
  nfl_ineligible: boolean;
  /** Sleeper taxi squad: never a lineup candidate (absent for everyone else). */
  on_taxi?: boolean;
}

export interface StartSitSummary {
  recommendation_strength?: RecommendationStrength | null;
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
  /** With `lineup_coverage`: whether every projection the gain compares exists (projection_complete is then false). */
  projections_complete?: boolean;
  /** Present when the league starts slots no engine fills: every total is partial (issue #253 G2, G3). */
  lineup_coverage?: LineupCoverage;
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
  /** signal-provenance.v1: lineup, projections and injury reports (lib/signal-provenance.ts). */
  provenance?: ProvenancePayload | null;
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
  projection_source_detail?: ProjectionSourceDetail;
  summary: StartSitSummary;
  actual_lineup: LineupPlayer[];
  recommended_lineup: LineupPlayer[];
  unconstrained_lineup: LineupPlayer[];
  decisions: StartSitDecision[];
  /** Absent from an older backend. */
  alternatives?: SlotAlternatives[];
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
