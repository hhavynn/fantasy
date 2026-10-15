export type Coverage = { eligible: number; evaluated: number; excluded_by_limit: number; limitation: string };
type Drop = { espn_player_id: number; name: string; position: string; sacrifice: string[] };
export type Decision = {
  action: string; actionable: boolean; confidence: string; risks: string[];
  roster_move: { status: string; summary: string; drop: Drop | null; alternatives: Drop[];
    active_count: number; capacity: number | null; position_limits: Record<string, number>;
    excluded: { espn_player_id: number; reason: string }[] };
  utility: { summary: string; stash: boolean; forward: { total_gain: number | null; weeks: { week: number; gain: number }[]; missing_weeks: number[] } };
  resources: { summary: string; alternative: { espn_player_id: number; name: string } | null };
  streaming: { summary: string; category: string; alternative_points: number | null; opponent_implied_total: number | null;
    team_implied_total: number | null; projection_synced_at: string | null;
    opponent_offense?: { games: number; sack_rate: number | null } | null;
    defense_performance?: { games: number; pass_epa_allowed: number | null; rush_epa_allowed: number | null } | null;
    weeks: { week: number; points: number | null; incumbent_points: number | null; opponent: string | null; bye: boolean }[] } | null;
};
export type WaiverRow = {
  espn_player_id: number; full_name: string; position: string; pro_team: string | null;
  availability_status: string; injury_status: string | null; percent_owned: number | null;
  opportunity_score: number | null; league_interest: string; recommendation: string; reasons: string[];
  competitors: { team_id: number; name: string; reasons: string[] }[];
  components: { player: { basis: string; basis_description?: string; value: number | null }; decision?: Decision };
};
