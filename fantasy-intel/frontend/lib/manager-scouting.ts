import type { Need } from "@/lib/types";

export type ScoutingSignal = {
  dimension: string; title: string; metric: string; classification: string;
  value: number | null; numerator: number | null; denominator: number | null;
  sample_size: number; min_sample: number; league_mean: number | null;
  confidence: string; rule: string; computed_at: string | null;
  evidence_status: string;
  stored_classification: { label: string; confidence: string; sample_size: number; computed_at: string | null } | null;
};

export type PositionAffinity = ScoutingSignal & {
  position: string; add_count: number | null; add_share: number | null;
  league_add_share: number | null; league_add_sample: number; league_team_samples: number;
  relative_ratio: number | null; ratio_rule: string; average_rostered: number | null;
  roster_history_sample: number; league_average_rostered: number | null;
  current_rostered: number | null; current_need_score: number | null;
};

export type ScoutingReport = {
  identity: {
    team_id: number; acquisitions: number | null; drops: number | null; completed_trades: number;
    observed_adds: number | null; observed_drops: number | null; is_faab: boolean | null;
    waiver_rank: number | null; faab_spent: number | null; faab_budget: number | null; faab_remaining: number | null;
  };
  headlines: { text: string; category: string; confidence: string; evidence: string[] }[];
  current_roster: {
    available: boolean; scoring_period: number; league_team_samples: number;
    synced_at: string | null; note: string; unknown_position_count: number | null;
    positions: { position: string; count: number | null; league_average: number | null; league_comparison: string }[];
  };
  position_affinity: PositionAffinity[];
  waiver_style: ScoutingSignal[];
  streaming_style: (ScoutingSignal & {
    position: string; distinct_starters: number | null; starter_turnover: number | null;
    turnover_changes: number | null; turnover_transitions: number | null; pickups: number | null;
  })[];
  current_needs: (Need & { league_starter_average: number | null; computed_at: string | null })[];
  trade_history: {
    completed_trades: number; players_sent: number; players_received: number;
    uneven_player_count_trades: number; trade_behavior_confidence: string;
    behavior: string; confidence_rule: string; note: string;
    recent_trades: {
      trade_id: string; date: string | null; sources: string[]; unequal_player_counts: boolean;
      players_sent: { espn_player_id: number; name: string | null; to_team_id: number | null }[];
      players_received: { espn_player_id: number; name: string | null; from_team_id: number | null }[];
    }[];
  };
  evidence: {
    metrics_computed_at: string | null; metrics_available: boolean; classifications_available: boolean;
    position_ratio_rule: string; trade_confidence_rule: string; streaming_definition: string; notes: string[];
  };
};
