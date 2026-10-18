// Types and pure helpers for the Projection performance page (GET /api/projections/performance).
// Every number and every verdict comes from the published report, which the backend builds offline from the
// research ESPN comparison (scripts/projection_performance.py). These helpers select, order and phrase those
// values; they never recompute a statistic or decide a verdict.

export const POSITIONS = ["QB", "RB", "WR", "TE"] as const;
export type Position = (typeof POSITIONS)[number];

export type Verdict = "insufficient_evidence" | "no_significant_difference" | "ours_better" | "espn_better";
export type DecisionVerdict = "insufficient_evidence" | "no_significant_difference" | "better" | "worse";
export type VersionVerdict = "insufficient_evidence" | "no_significant_difference" | "first_better" | "second_better";

export type ErrorStats = { n: number; mae: number | null; rmse: number | null; bias: number | null };
export type Interval = [number, number] | null;

export type LeadTime = {
  n: number; ours_median_h: number | null; espn_median_h: number | null; median_gap_h: number | null;
  horizon_matched_n: number; horizon_matched_share: number | null; window_h: number;
};

export type PositionSummary = {
  model_version: string; verdict: Verdict; verdict_reason: string; statement: string;
  fully_final_weeks: number; required_weeks: number; required_n: number; paired_n: number; n_model: number;
  ours: ErrorStats; espn: ErrorStats; mae_diff: number | null; ci95: Interval;
  coverage: { eligible_resolved: number; model_covered: number; fallback_n: number; fallback_share: number | null; model_share: number | null };
  lead_time: Partial<LeadTime>;
  conditional: { n: number; weeks_covered: number; ours: ErrorStats; espn: ErrorStats; mae_diff: number | null; ci95: Interval;
    verdict: Verdict; verdict_reason: string; statement: string };
  observations: { rows: number; scored: number; excluded: number; by_reason: Record<string, number> };
};

export type ComparisonGroup = {
  n: number; n_model: number; weeks_covered: number; ours: ErrorStats; espn: ErrorStats; mae_diff: number | null;
  ci95: Interval; verdict?: Verdict; descriptive_verdict?: Verdict; fallback_share?: number | null;
};
export type Comparison = {
  name: string; definition: string; verdict: Verdict; verdict_reason: string; n: number;
  by_position: Record<string, ComparisonGroup>; fallback_share?: number | null;
};

export type RbVolumeGroupKey = "low_volume" | "volume" | "no_recent_games";
export type RbVolumeGroup = {
  n: number; weeks_covered: number; required_n: number; required_weeks: number; ours: ErrorStats; espn: ErrorStats;
  mae_diff: number | null; ci95: Interval; verdict: Verdict; verdict_reason: string; statement: string;
};
export type RbVolumeSplit = {
  threshold: number; groups: RbVolumeGroupKey[]; definition: string; source: string;
  comparisons: Record<"pure_independent" | "conditional", { n: number; ours_column: string; by_group: Record<RbVolumeGroupKey, RbVolumeGroup> }>;
};

export type WeeklyError = { week: number; position: Position; n: number; ours_mae: number | null; espn_mae: number | null; model_version: string };

export type DecisionSection = {
  decisions: number; weeks: number; unique_player_pairs: number; ours_accuracy: number | null; espn_accuracy: number | null;
  difference: number | null; ci95: Interval; verdict: DecisionVerdict; statement: string;
};
export type DecisionModel = {
  model_version: string; evaluated: boolean; with_pregame_prediction: number; candidates: number; unavailable_reason?: string;
  same_position?: DecisionSection; flex?: DecisionSection; close_calls?: DecisionSection;
  lineups?: { decisions: number; weeks: number; ours_mean_regret: number | null; espn_mean_regret: number | null;
    regret_difference: number | null; ci95: Interval; verdict: DecisionVerdict; statement: string };
};
export type StartSitSummary = {
  source: string; sets: number; audit_only_ignored: number; weeks_recorded: number[];
  gate: { min_decisions: number; min_weeks: number }; population: string;
  models: Record<string, DecisionModel>; warnings: string[];
};

export type VersionComparison = {
  versions: [string, string]; diff: string; scored_weeks: Record<string, number[]>; common_weeks: number[]; n: number; rule: string;
  by_position: Record<string, { n: number; weeks_covered: number; required_n: number; required_weeks: number;
    mae: Record<string, number | null>; mae_diff: number | null; ci95: Interval; verdict?: VersionVerdict;
    descriptive_verdict?: VersionVerdict; verdict_reason?: string; descriptive_verdict_reason?: string }>;
};

export type PerformanceReport = {
  schema_version: string; synthetic: boolean; label?: string; season: number; generated_at: string;
  data_as_of: Record<string, string | null>;
  production: { production_version: string; fallback_version: string; benchmarked_version: string; production_approval: string;
    espn_superiority_established: boolean; start_sit_ready: boolean; status_note: string | null; production_evidence: string };
  evidence_gate: { min_weeks: number; min_matched_per_position: number; interval: string; resamples: number; seed: number;
    horizon_window_h: number; start_sit_min_decisions: number; start_sit_min_weeks: number };
  weeks: { with_espn_history: number[]; fully_final: number[]; partial: number[]; in_progress: number[]; upcoming: number[] };
  benchmark: { schema_version: string; available: boolean; unavailable_reason: string | null; forecast_source: string | null; content_sha256: string };
  status: { verdict: Verdict; verdict_reason: string | null; position_verdicts: Record<string, Verdict> };
  positions: Record<Position, PositionSummary>;
  comparisons: Record<string, Comparison>;
  weekly: WeeklyError[];
  /** Absent from reports built before the split existed. */
  rb_volume_split?: RbVolumeSplit;
  preliminary: { weeks: number[]; note: string | null; by_position: Record<string, { n: number; ours: ErrorStats; espn: ErrorStats }> };
  model_versions: {
    production: string; fallback: string;
    served_now: { week: number; served_version: string | null; fallback: boolean; note: string | null;
      forecast_horizon?: number | null; horizon_note?: string | null }[];
    pregame_by_week: Record<string, { week: number; paired_pregame_forecasts: number; scored: number }[]>;
    comparison: VersionComparison;
  };
  start_sit: StartSitSummary;
  coverage: { rows: number; scored: number; by_exclusion_reason: Record<string, number>; by_outcome_status: Record<string, number>;
    scored_by_origin: Record<string, number>; forecast_runs_excluded_by_reason: Record<string, number>;
    excluded_runs: { run_id: string; week: number; reason: string }[] };
  notes: string[];
};

export type PerformanceResponse = {
  schema_version: string; season: number; available: boolean; status: "ok" | "stale" | "no_report" | "integrity_failed";
  unavailable_reason: string | null; report: PerformanceReport | null; report_sha256: string | null;
  stale: boolean; stale_reasons: string[]; rejected: { report_sha256: string; generated_at: string | null; reason: string }[];
  governance: { production_version: string; fallback_version: string; espn_superiority_established: boolean; start_sit_ready: boolean;
    status_note: string | null; production_evidence: string };
};

/** The one sentence per verdict. Must equal the backend's STATEMENTS (the fixture test checks it). */
export const VERDICT_TEXT: Record<Verdict, string> = {
  insufficient_evidence: "Insufficient prospective evidence.",
  no_significant_difference: "No statistically established difference.",
  ours_better: "Fantasy Intel was more accurate than ESPN.",
  espn_better: "ESPN was more accurate than Fantasy Intel.",
};
export const DECISION_TEXT: Record<DecisionVerdict, string> = {
  insufficient_evidence: "Insufficient prospective evidence.",
  no_significant_difference: "No statistically established difference.",
  better: "Fantasy Intel made better decisions than ESPN.",
  worse: "ESPN made better decisions than Fantasy Intel.",
};

/** Short badge text and tone. Both directions get the same weight; "no verdict" states stay neutral. */
export const VERDICT_BADGE: Record<Verdict, { text: string; tone: string }> = {
  insufficient_evidence: { text: "Insufficient evidence", tone: "" },
  no_significant_difference: { text: "No established difference", tone: "" },
  ours_better: { text: "Ours more accurate", tone: "good" },
  espn_better: { text: "ESPN more accurate", tone: "bad" },
};
export const DECISION_BADGE: Record<DecisionVerdict, { text: string; tone: string }> = {
  insufficient_evidence: { text: "Insufficient evidence", tone: "" },
  no_significant_difference: { text: "No established difference", tone: "" },
  better: { text: "Ours better", tone: "good" },
  worse: { text: "ESPN better", tone: "bad" },
};

export function versionVerdictText(v: VersionVerdict | undefined, versions: [string, string]): string {
  switch (v) {
    case "first_better": return `${versions[0]} more accurate`;
    case "second_better": return `${versions[1]} more accurate`;
    case "no_significant_difference": return "No established difference";
    default: return "Insufficient evidence";
  }
}

/** Plain-language exclusion reasons (backend reason codes are never shown raw). */
export const EXCLUSION_LABEL: Record<string, string> = {
  unsupported_position: "Position not modelled (K, D/ST)",
  uncertain_id_mapping: "Player identity uncertain",
  team_unknown: "Team unknown",
  bye: "Bye week",
  kickoff_unknown: "Kickoff time unknown",
  no_espn_snapshot: "No ESPN projection stored",
  espn_not_pregame: "ESPN projection taken after kickoff",
  no_independent_forecast_for_week: "No pregame forecast of the model that week",
  independent_not_pregame: "Model forecast made after kickoff",
  independent_model_mismatch: "Other model version",
  independent_scoring_mismatch: "Made under other league scoring",
  independent_artifact_time_unknown: "Model fit time unknown",
  independent_artifact_after_first_kickoff: "Model fitted after the week began",
  independent_id_mismatch: "Forecast names another player",
  outcome_pending_game: "Game not played yet",
  outcome_missing_ingestion: "Box score not synced yet",
  outcome_bye: "Bye (no game)",
  outcome_unresolved_identity: "Outcome identity unresolved",
  week_not_fully_final: "Week not fully final (preliminary)",
};

export function exclusionLabel(code: string): string {
  return EXCLUSION_LABEL[code] ?? code.replace(/_/g, " ");
}

/** A signed number with a real minus sign, so direction never depends on colour ("−0.42", "+1.10"). */
export function signedNum(v: number | null | undefined, digits = 2): string | null {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  const s = Math.abs(v).toFixed(digits);
  if (Number(s) === 0) return (0).toFixed(digits);
  return `${v > 0 ? "+" : "−"}${s}`;
}

export function intervalText(ci: Interval, digits = 2): string | null {
  if (!ci) return null;
  return `${signedNum(ci[0], digits)} to ${signedNum(ci[1], digits)}`;
}

/** "Ours made 24 h earlier" for a median gap in hours (ours − ESPN; positive = our forecast earlier). */
export function leadGapText(gap: number | null | undefined): string | null {
  if (gap === null || gap === undefined || Number.isNaN(gap)) return null;
  if (Math.abs(gap) < 0.5) return "same time as ESPN";
  const h = Math.abs(gap) >= 10 ? Math.round(Math.abs(gap)).toString() : Math.abs(gap).toFixed(1);
  return gap > 0 ? `${h} h earlier than ESPN` : `${h} h later than ESPN`;
}

/** Ours vs ESPN MAE per fully final week for one position; null where the week has no paired rows (a gap, not 0). */
export function weeklySeries(report: PerformanceReport, pos: Position): { weeks: number[]; ours: (number | null)[]; espn: (number | null)[]; n: number[] } {
  const weeks = report.weeks.fully_final;
  const at = (w: number) => report.weekly.find((r) => r.week === w && r.position === pos);
  return {
    weeks,
    ours: weeks.map((w) => at(w)?.ours_mae ?? null),
    espn: weeks.map((w) => at(w)?.espn_mae ?? null),
    n: weeks.map((w) => at(w)?.n ?? 0),
  };
}

export type IntervalRow = { label: string; value: number | null; lo: number | null; hi: number | null; verdict: Verdict; muted: boolean; n: number };

/** Rows for the paired-difference chart, straight from the report: the point and both bounds as published. */
export function intervalRows(report: PerformanceReport): IntervalRow[] {
  return POSITIONS.map((pos) => {
    const p = report.positions[pos];
    return {
      label: pos, value: p.mae_diff, lo: p.ci95?.[0] ?? null, hi: p.ci95?.[1] ?? null, verdict: p.verdict,
      muted: p.verdict === "insufficient_evidence", n: p.paired_n,
    };
  });
}

export type Headline = { tone: "info" | "caution" | "positive" | "negative"; title: string; body: string };

/** The status line that opens the page: the backend's summary verdict, never a judgement of our own. */
export function headline(res: PerformanceResponse): Headline {
  const r = res.report;
  if (!res.available || !r) {
    return {
      tone: res.status === "integrity_failed" ? "caution" : "info",
      title: res.status === "integrity_failed" ? "No verified report" : "No accuracy report yet",
      body: res.unavailable_reason ?? "No projection-performance report is available.",
    };
  }
  const verdicts = POSITIONS.map((p) => r.positions[p].verdict);
  const decided = POSITIONS.filter((p) => r.positions[p].verdict !== "insufficient_evidence");
  const listed = (v: Verdict) => POSITIONS.filter((p) => r.positions[p].verdict === v).join(", ");
  if (decided.length === 0) {
    const weeks = r.weeks.fully_final.length;
    return {
      tone: "info",
      title: "Still collecting weeks",
      body: `No position has reached the evidence bar yet (at least ${r.evidence_gate.min_weeks} fully final weeks and ` +
        `${r.evidence_gate.min_matched_per_position} matched observations per position). ` +
        `${weeks === 0 ? "No week is fully final yet." : `${weeks} fully final ${weeks === 1 ? "week" : "weeks"} so far.`} ` +
        "The head-to-head call against ESPN shows up here once a position clears it.",
    };
  }
  const parts: string[] = [];
  if (verdicts.includes("ours_better")) parts.push(`more accurate than ESPN at ${listed("ours_better")}`);
  if (verdicts.includes("espn_better")) parts.push(`less accurate than ESPN at ${listed("espn_better")}`);
  if (verdicts.includes("no_significant_difference")) parts.push(`no established difference at ${listed("no_significant_difference")}`);
  const rest = POSITIONS.filter((p) => r.positions[p].verdict === "insufficient_evidence");
  const tone = r.status.verdict === "ours_better" ? "positive" : r.status.verdict === "espn_better" ? "negative" : "info";
  const mixed = r.status.verdict === "no_significant_difference" && (verdicts.includes("ours_better") || verdicts.includes("espn_better"));
  return {
    tone,
    title: mixed ? "Mixed results by position" : VERDICT_TEXT[r.status.verdict].replace(/\.$/, ""),
    body: `Fantasy Intel was ${parts.join("; ")}.${rest.length ? ` ${rest.join(", ")}: insufficient prospective evidence.` : ""}`,
  };
}

/** Exclusion reasons with counts, largest first, for one position or the whole report. */
export function exclusionRows(byReason: Record<string, number>): { code: string; label: string; n: number }[] {
  return Object.entries(byReason)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([code, n]) => ({ code, label: exclusionLabel(code), n }));
}

/** Start/Sit model sets in display order: the production model first, then the fallback. */
export function decisionModels(s: StartSitSummary, production: string): [string, DecisionModel][] {
  return Object.entries(s.models).sort(([, a], [, b]) =>
    Number(b.model_version === production) - Number(a.model_version === production) || a.model_version.localeCompare(b.model_version));
}

/** "1–5" for a run of consecutive weeks, "2, 4, 5" otherwise, "none" when empty. */
/** Plain-language names for the RB volume groups (backend codes are never shown raw). */
export function rbVolumeLabel(group: RbVolumeGroupKey, threshold: number): string {
  if (group === "low_volume") return `Under ${threshold} opportunities a game`;
  if (group === "volume") return `${threshold}+ opportunities a game`;
  return "No earlier game this season";
}

export type RbVolumeRow = { group: RbVolumeGroupKey; label: string } & RbVolumeGroup;

/** Rows of the RB-by-volume table in the report's group order, the published figures unchanged; [] for a report
 *  built before the split existed. */
export function rbVolumeRows(report: PerformanceReport, comparison: "pure_independent" | "conditional"): RbVolumeRow[] {
  const s = report.rb_volume_split;
  if (!s) return [];
  return s.groups.map((g) => ({ group: g, label: rbVolumeLabel(g, s.threshold), ...s.comparisons[comparison].by_group[g] }));
}

export function weeksText(weeks: number[]): string {
  if (weeks.length === 0) return "none";
  const run = weeks.length > 2 && weeks.every((w, i) => i === 0 || w === weeks[i - 1] + 1);
  return run ? `${weeks[0]}–${weeks[weeks.length - 1]}` : weeks.join(", ");
}
