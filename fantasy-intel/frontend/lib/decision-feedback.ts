// Decision feedback beside the weekly recap (issue #194): response types for GET /api/weekly-recap/feedback and pure
// view helpers. Nothing here computes a grade: every number comes from the backend
// (app/analytics/decision_feedback.py). No `@/` value imports, so it runs under the unit-test runner.
import { plural } from "./plural.ts";

export type DfSource = "espn" | "independent";
export const DF_SOURCES: readonly DfSource[] = ["espn", "independent"];

export type DfInput = {
  source: string;
  snapshot_id: string | null;
  captured_at: string | null;
  kickoff: string | null;
  model_version: string | null;
  scoring_version: string | null;
  content_sha256: string | null;
  artifact_id: string | null;
  coverage_status: string | null;
};

export type DfSide = {
  espn_player_id: number;
  full_name: string;
  position: string | null;
  pro_team: string | null;
  forecast: number | null;
  actual: number | null;
  input: DfInput;
};

export type DfCost = { lower: number; upper: number | null; upper_unknown: number };
export type DfVerdict = "correct" | "missed" | "push";

export type DfCall = {
  week: number;
  slot: string;
  start: DfSide;
  over: DfSide;
  forecast_gap: number;
  close_call: boolean;
  verdict: DfVerdict | null;
  margin: number | null;
  opportunity_cost: DfCost | null;
  excluded: string | null;
  alternatives: number;
};

export type DfTally = {
  calls: number;
  graded: number;
  excluded: number;
  correct: number;
  missed: number;
  push: number;
  hit_rate: number | null;
  close_calls_missed: number;
  opportunity_cost_lower: number | null;
  opportunity_cost_upper: number | null;
};

export type DfRosterInput = { snapshot_id: string; captured_at: string; cutoff: string; source: string };

export type DfLineup = {
  complete: boolean;
  missing_outcomes: number;
  archived: number | null;
  best: number | null;
  archive_shortfall: number | null;
  yours: number | null;
  yours_outside_decision_set: number | null;
  archived_minus_yours: number | null;
  slots: string[];
};

export type DfPlayer = DfSide & { error: number; started_in_archive: boolean; week: number };

export type DfWeek = {
  week: number;
  status: "graded" | "no_archive";
  reason: string | null;
  roster_input: DfRosterInput | null;
  calls: DfCall[];
  lineup: DfLineup | null;
  exclusions: Record<string, number>;
  coverage: { decision_set: number; without_forecast: number; share: number | null } | null;
  forecast_error: { n: number; mae: number | null; bias: number | null } | null;
  summary: DfTally;
};

export type DfSeasonWeek = DfTally & {
  matchup_period: number;
  week: number;
  status: "graded" | "no_archive";
  coverage: number | null;
  decision_set: number;
  archive_shortfall: number | null;
  forecast_mae: number | null;
  forecast_n: number;
};

export type DecisionFeedback = {
  schema: string;
  rule_version: string;
  status: "ok" | "no_archive" | "no_team" | "no_completed_week" | "weeks_unmapped";
  message: string | null;
  season: number;
  selected_week: number | null;
  team: { team_id: number; name: string | null } | null;
  source: {
    key: DfSource;
    label: string;
    is_default: boolean;
    covered_positions: string[];
    unavailable_reason: string | null;
    versions: Record<string, string>;
  };
  definitions: Record<string, string>;
  exclusion_reasons: Record<string, string>;
  scoring_periods: number[];
  weeks: DfWeek[];
  summary?: DfTally;
  feedback: {
    worked: DfCall[];
    changed: DfCall[];
    watch: { above_forecast: DfPlayer[]; below_forecast: DfPlayer[]; notes: string[] };
  } | null;
  season_to_date: { weeks: DfSeasonWeek[]; totals: DfTally & { weeks: number; weeks_graded: number; forecast_mae: number | null; forecast_n: number } } | null;
};

const first = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

/** `?source=` for the feedback: a known source, else null (the league's default). */
export function parseFeedbackSource(sp: Record<string, string | string[] | undefined>): DfSource | null {
  const v = first(sp.source);
  return (DF_SOURCES as readonly string[]).includes(v ?? "") ? (v as DfSource) : null;
}

/** Backend path: the recap's week and the chosen source. Never a team: the route always reads the viewer's own. */
export function feedbackPath(week: number | null, source: DfSource | null): string {
  const p = new URLSearchParams();
  if (week !== null) p.set("week", String(week));
  if (source !== null) p.set("source", source);
  const s = p.toString();
  return s ? `/api/weekly-recap/feedback?${s}` : "/api/weekly-recap/feedback";
}

/** Points with two decimals; null gives "—". */
export function pts(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v.toFixed(2);
}

/** Opportunity-cost bounds in words: "0.00", "5.00 to 7.00" or "at least 5.00" when the upper bound is unknown. */
export function costText(c: DfCost | null): string {
  if (!c) return "—";
  if (c.upper === null) return `at least ${c.lower.toFixed(2)}`;
  return c.lower === c.upper ? c.lower.toFixed(2) : `${c.lower.toFixed(2)} to ${c.upper.toFixed(2)}`;
}

/** Season or week totals of the bounds: "12.00 to 17.00", or "at least 12.00". */
export function costRangeText(lower: number | null, upper: number | null): string {
  if (lower === null) return "—";
  if (upper === null) return `at least ${lower.toFixed(2)}`;
  return lower === upper ? lower.toFixed(2) : `${lower.toFixed(2)} to ${upper.toFixed(2)}`;
}

export const VERDICT_LABEL: Record<DfVerdict, string> = { correct: "Worked", missed: "Missed", push: "Push" };
export const VERDICT_TONE: Record<DfVerdict, string> = { correct: "good", missed: "bad", push: "warn" };

/** "1 of 3 decided calls worked" with pushes and excluded calls named; never a percentage without its count. */
export function tallyText(t: DfTally): string {
  const decided = t.correct + t.missed;
  if (t.graded === 0) return t.calls === 0 ? "No calls to grade" : `${t.calls} ${plural(t.calls, "call")}, none with recorded points for both players`;
  const parts = [`${t.correct} of ${decided} decided ${plural(decided, "call")} worked`];
  if (t.push) parts.push(`${t.push} ${plural(t.push, "push", "pushes")}`);
  if (t.excluded) parts.push(`${t.excluded} left ungraded`);
  return parts.join(", ");
}

/** The share as a whole percent with its sample: "33% of 3". */
export function hitRateText(t: DfTally): string {
  const decided = t.correct + t.missed;
  return t.hit_rate === null ? "—" : `${Math.round(t.hit_rate * 100)}% of ${decided}`;
}

/** A call in one line: "Start Ann QB over Bea QB (QB)". */
export function callLine(c: DfCall): string {
  return `Start ${c.start.full_name} over ${c.over.full_name} (${c.slot})`;
}

/** The outcome of a graded call: "18.00 vs 25.00". */
export function outcomeText(c: DfCall): string {
  return `${pts(c.start.actual)} vs ${pts(c.over.actual)}`;
}

/** UTC timestamp as "2026-09-10 12:00 UTC" (inputs are compared in UTC; no local-time ambiguity). */
export function utcText(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown";
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

/** Lines that say exactly which archived input produced a forecast. */
export function inputLines(i: DfInput): string[] {
  const out = [`${i.source} snapshot ${i.snapshot_id ?? "unknown"}`, `captured ${utcText(i.captured_at)}, kickoff ${utcText(i.kickoff)}`];
  if (i.model_version) out.push(`model ${i.model_version}`);
  if (i.scoring_version) out.push(`scoring ${i.scoring_version}`);
  if (i.content_sha256) out.push(`content ${i.content_sha256.slice(0, 16)}`);
  return out;
}

/** Exclusion counts as sentences, largest first, with the backend's reason text. */
export function exclusionLines(counts: Record<string, number>, reasons: Record<string, string>): string[] {
  return Object.entries(counts)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([k, n]) => `${n} ${plural(n, "player")}: ${reasons[k] ?? k}`);
}
