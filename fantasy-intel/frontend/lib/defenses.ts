// Types and pure helpers for the D/ST projections page (GET /api/projections/defenses, dfp-r1).

export type DstStatLine = {
  sacks: number | null;
  interceptions: number | null;
  fumble_recoveries: number | null;
  blocked_kicks?: number | null;
  safeties?: number | null;
  return_tds?: number | null;
  points_allowed: number | null;
  yards_allowed: number | null;
  p_any_td: number | null;
  td_points?: number | null;
};

export type DstContext = {
  spread_line: number | null;
  total_line: number | null;
  opponent_implied_total: number | null;
  expected_margin?: number | null;
  has_line?: boolean;
  kickoff_utc?: string | null;
};

export type Explanation = { feature: string; contribution: number | null; text: string };

export type DstPlayer = {
  espn_player_id: number;
  name: string | null;
  nfl_team: string | null;
  opponent: string | null;
  is_home: boolean | null;
  fantasy_team_id: number | null;
  independent: {
    projected_points: number | null;
    expected_points: number | null;
    interval_80: { low: number; high: number; nominal_coverage: number } | null;
    coverage_status: string;
    stat_line: DstStatLine | null;
    context: DstContext | null;
    explanation: Explanation[];
    flags: string[];
    missing_inputs: string[];
    pregame: boolean | null;
    model_version: string;
  };
  espn: { projected_points: number | null };
  comparison: { expected_minus_espn: number | null };
};

export type DstResponse = {
  schema_version: string;
  league: { id: number; season: number };
  week: number;
  available: boolean;
  unavailable_reason: string | null;
  governance: { model_version: string; status_note: string };
  notes: string[];
  run: null | {
    run_id: string;
    model_version: string;
    created_at: string;
    artifact_verified: boolean;
    artifact_note: string | null;
    feature_cutoff: { data_through_season: number | null; data_through_week: number | null };
    nfl_synced_at: string | null;
    forecast_horizon: number;
  };
  players: DstPlayer[];
};

const STATUS_TEXT: Record<string, string> = {
  bye: "Bye week",
  no_nfl_team: "No NFL team",
  no_estimate: "No estimate",
};

/** Why a D/ST has no projection, in a few words. */
export function statusText(status: string): string {
  return STATUS_TEXT[status] ?? status.replace(/_/g, " ");
}

/** Projected D/STs first (by expected points), then byes and the rest by team. */
export function splitDefenses(players: DstPlayer[]): { projected: DstPlayer[]; other: DstPlayer[] } {
  const projected = players
    .filter((p) => p.independent.coverage_status === "projected" && p.independent.expected_points !== null)
    .sort((a, b) => (b.independent.expected_points ?? 0) - (a.independent.expected_points ?? 0));
  const other = players.filter((p) => !projected.includes(p))
    .sort((a, b) => (a.nfl_team ?? "").localeCompare(b.nfl_team ?? ""));
  return { projected, other };
}

/** "Favored by 7 · total 41.5" from the line the run used; null without one. */
export function lineText(ctx: DstContext | null): string | null {
  if (!ctx || ctx.spread_line === null || ctx.total_line === null || ctx.expected_margin === undefined || ctx.expected_margin === null) {
    return null;
  }
  const m = ctx.expected_margin;
  const side = m > 0 ? `Favored by ${fmt(m)}` : m < 0 ? `${fmt(-m)}-pt underdog` : "Pick'em";
  return `${side} · total ${fmt(ctx.total_line)}`;
}

/** Takeaways = interceptions + fumble recoveries. */
export function takeaways(s: DstStatLine | null): number | null {
  if (!s || s.interceptions === null || s.fumble_recoveries === null) return null;
  return s.interceptions + s.fumble_recoveries;
}

/** The headline sentence the model wrote, if any. */
export function headline(p: DstPlayer): string | null {
  return p.independent.explanation.find((e) => e.feature === "headline")?.text ?? null;
}

function fmt(v: number): string {
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}

/** Expected sacks + takeaways: how often the defense makes a splash play (the chart's "disruption"). */
export function disruption(s: DstStatLine | null): number | null {
  const t = takeaways(s);
  if (!s || s.sacks === null || t === null) return null;
  return s.sacks + t;
}

/** Middle value of a list (mean of the two middle values when even); null when empty. */
export function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const v = [...values].sort((a, b) => a - b);
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}
