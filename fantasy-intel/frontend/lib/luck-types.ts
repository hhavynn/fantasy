// Luck vs. Skill Lab API types (GET /api/luck-lab). Kept in their own module.

export type LlResult = "W" | "L" | "T";
export type LlRecord = { wins: number; losses: number; ties: number; win_equivalents: number };
export type LlBrief = {
  matchup_period: number; weeks: number[] | null; score: number; rank: number; teams: number; opponent_id: number;
  opponent_score: number; opponent_rank: number; margin: number; result: LlResult; flags: string[];
};
export type LlSummary = {
  team_id: number; periods: number; name: string; is_mine: boolean;
  espn_record: { wins: number | null; losses: number | null; ties: number | null };
  espn_points_for: number | null; espn_points_against: number | null;
  actual?: LlRecord; expected_wins?: number; expected_losses?: number; schedule_luck?: number;
  all_play?: { wins: number; losses: number; ties: number; pct: number | null };
  scoring?: { total: number; mean: number; median: number; min: number; max: number; range: number; q1: number | null;
    q3: number | null; iqr: number | null; top_half_pct: number; first_place: number; last_place: number };
  difficulty?: { opponent_points_total: number; opponent_points_mean: number; opponent_share_mean: number;
    opponents_top_band: number; opponents_bottom_band: number; opponent_season_share_mean: number | null;
    hardest: LlBrief; easiest: LlBrief };
  notable?: { brutal_losses: LlBrief[]; fortunate_wins: LlBrief[] };
};
export type LlLeader = {
  team_id: number; name: string; is_mine: boolean; luck_rank: number; schedule_luck: number; actual: LlRecord;
  expected_wins: number; expected_wins_rank: number; all_play: { wins: number; losses: number; ties: number; pct: number | null };
  espn_record: { wins: number | null; losses: number | null; ties: number | null }; scoring_mean: number; scoring_rank: number;
  opponent_points_mean: number; difficulty_rank: number; periods: number;
};
export type LlTimeline = LlBrief & { expected_wins: number; luck: number; cumulative_luck: number; median: number; opponent_name: string };
export type LlCfRow = {
  matchup_period: number; weeks: number[] | null; score: number; actual_opponent_id: number; actual_opponent_score: number;
  actual_result: LlResult; substituted_opponent_id: number; substituted_opponent_score: number; counterfactual_result: LlResult;
  margin: number; changed: boolean;
};
export type LlCounterfactual = {
  team_id: number; schedule_of: number; periods_compared: number[]; excluded: { matchup_period: number; reason: string }[];
  actual: LlRecord; counterfactual: LlRecord; win_equivalent_difference: number; actual_pct: number | null;
  counterfactual_pct: number | null; flips: { loss_to_win: number[]; win_to_loss: number[]; other_changes: number[] };
  rows: LlCfRow[];
};
export type LlScheduleRow = Omit<LlCounterfactual, "rows" | "team_id"> & { name: string; same_periods_as_all_eligible: boolean };
export type LuckLab = {
  league: { league_id: number; season: number; name: string; current_week: number; reg_season_matchups: number | null };
  teams: { team_id: number; name: string; is_mine: boolean }[];
  focus_team_id: number; schedule_of: number | null;
  coverage: { teams: number; eligible_periods: number[];
    excluded_periods: { matchup_period: number; weeks: number[] | null; kind: "not_completed" | "incomplete"; teams_with_final_scores: number;
      teams: number; reasons: string[] }[];
    data_notes: { duplicate_ids_merged: number[]; conflicting_ids: number[]; missing_ids: number; no_period: (number | null)[]; playoff_rows: number };
    rule: string };
  leaderboard: LlLeader[];
  summaries: Record<string, LlSummary>;
  spotlight: LlSummary & { luck_rank: number | null; scoring_rank: number | null; difficulty_rank: number | null; teams_ranked: number };
  timeline: LlTimeline[]; biggest_contributors: LlTimeline[];
  counterfactual: LlCounterfactual | null; all_schedules: LlScheduleRow[];
  matrix: { periods: { matchup_period: number; weeks: number[] | null }[];
    teams: { team_id: number; name: string; cells: { matchup_period: number; rank: number; result: LlResult; score: number;
      expected_wins: number; flags: string[] }[] }[] };
  rules: { expected: string; luck: string; eligible: string; counterfactual: string; bands: string; band_size: number; result_source: string };
  notes: string[];
};
