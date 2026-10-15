// Pure helpers for the independent-projection promotion tracker on the Data quality page.
// They only format and compare numbers the backend returned (GET /api/projections/independent/promotion);
// the criteria themselves live in the backend's frozen PROMOTION_CRITERIA.

export type ModelEvidence = {
  status: string | null; start_sit_ready: boolean | null; reasons: string[]; heldout_n: number | null;
  relative_mae_improvement: number | null; best_trailing_baseline: string | null; mae_primary: number | null;
  mae_best_baseline: number | null; ci95_primary_minus_baseline: [number, number] | null;
};
export type EspnEvidence = {
  n: number; pregame_completed: number; required: number; criterion_met: boolean;
  mae_independent: number | null; mae_espn: number | null;
};
export type PregameSnapshot = {
  week: number; first_kickoff: string | null; latest_pregame_snapshot_at: string | null;
  latest_snapshot_at: string | null; captured: boolean; players: number;
};
/** The hosted API bundle has no research packages and answers `available: false` with a reason only. */
export type PromotionResponse =
  | ({ available: true; unavailable_reason: null } & PromotionTracker)
  | { schema_version: string; available: false; unavailable_reason: string };

export type PromotionTracker = {
  schema_version: string; season: number; current_week: number; notes: string[]; model_version: string;
  criteria: {
    heldout_min_observations_per_position: number;
    min_relative_mae_improvement_vs_best_trailing_baseline: number;
    require_bootstrap_ci_upper_below_zero: boolean;
    espn_min_matched_per_position: number;
    espn_rule: string;
  };
  run: { run_id: string; week: number; model_version: string; created_at: string } | null;
  run_missing_reason: string | null;
  espn: { available: boolean; unavailable_reason: string | null; matched_observations: number;
    by_week: { week: number; pregame_completed: number; matched: number }[] };
  pregame_snapshot: PregameSnapshot;
  positions: { position: string; model: ModelEvidence; espn: EspnEvidence }[];
};

/** Share of the required matched observations collected so far, 0..1 (for a progress bar). */
export function matchedProgress(n: number, required: number): number {
  if (!(required > 0)) return 0;
  return Math.min(1, Math.max(0, n / required));
}

/** Signed percentage of a relative MAE improvement (0.0578 -> "+5.8%", -0.0056 -> "−0.6%"). */
export function signedPct(v: number | null, digits = 1): string | null {
  if (v === null || Number.isNaN(v)) return null;
  const s = (Math.abs(v) * 100).toFixed(digits);
  if (Number(s) === 0) return `${(0).toFixed(digits)}%`;
  return `${v > 0 ? "+" : "−"}${s}%`;
}

/** Whether a stored improvement clears the frozen bar; null when there is no stored result. */
export function clearsBar(v: number | null, bar: number): boolean | null {
  return v === null || Number.isNaN(v) ? null : v >= bar;
}

export const STATUS_LABEL: Record<string, { text: string; tone: string }> = {
  validated: { text: "Validated", tone: "good" },
  experimental: { text: "Experimental", tone: "warn" },
  insufficient_data: { text: "Insufficient data", tone: "" },
};
