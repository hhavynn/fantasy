import type {
  ContextEvidence, ContextLabels, ForwardProjection, PlayerCore, RouteData, TrendInfo, UsageWeek,
} from "@/lib/types";
import type { ProvenancePayload } from "@/lib/signal-provenance";

/** A row of `GET /api/players?q=`. */
export type PlayerListRow = PlayerCore & { fantasy_team: string | null };

export type Injury = {
  week: number; report_status: string | null; report_primary_injury: string | null; practice_status: string | null;
};

/** `GET /api/players/{id}` (only the fields the page reads). */
export type Detail = {
  player: PlayerCore & { mapping_notes: string | null; nfl_name: string | null; nfl_team: string | null };
  score: {
    components: {
      trends: TrendInfo[]; opportunity_basis: string; recent_weeks: number[]; value_formula: string;
      recent_opportunity_per_game: number | null; recent_ppr_per_game: number | null;
    };
    reasons: string[]; as_of_week: number;
  } | null;
  weekly: UsageWeek[];
  league_points: { scoring_period: number; actual_points: number | null; projected_points: number | null }[];
  forward_projection: ForwardProjection | null;
  injuries: Injury[];
  depth_chart: { dt: string; team: string; pos_abb: string; pos_rank: number } | null;
  waiver: { recommendation: string; waiver_score: number; reasons: string[];
    /** roster.fit_status "partial": the score lacks its lineup-gain term (shown as unknown, lib/waivers knownWaiverScore). */
    components?: { roster?: { fit_status?: string; value_unknown?: { name: string | null }[] } | null } | null } | null;
  unavailable_metrics: Record<string, string>;
  /** signal-provenance.v1: league data, forward projections, usage and injury reports (lib/signal-provenance.ts). */
  provenance?: ProvenancePayload | null;
  route_data: RouteData | null;
  context: { labels: ContextLabels; evidence: ContextEvidence } | null;
  team_context: {
    proe: number | null; neutral_pass_rate: number | null; sack_rate: number | null;
    hit_or_sack_rate: number | null; pfr_pressure_rate: number | null; next_opponent: string | null;
    next_implied_total: number | null; games: number; through_week: number;
  } | null;
  opponent_defense: {
    defteam: string; games: number; pass_epa_allowed: number | null; rush_epa_allowed: number | null;
    rush_success_allowed: number | null; explosive_run_rate_allowed: number | null;
    rb_target_share_allowed: number | null; te_target_share_allowed: number | null;
    ranks: Record<string, number | null>; pass_plays: number; rush_plays: number;
  } | null;
};
