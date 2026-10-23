// Pure helpers for the waivers page: sort keys, ordinal ranks for labelled values, and the
// "top claims" selection. No React and no `@/` imports, so they run under the unit-test runner.

export const WAIVER_SORT_KEYS = ["rank", "score", "trend", "owned", "competition"] as const;
export type WaiverSortKey = (typeof WAIVER_SORT_KEYS)[number];

/** Role trend as an orderable number: rising > flat > falling. Insufficient or unknown data has no rank (sorts last). */
export function trendRank(trend: string | null | undefined): number | null {
  switch (trend) {
    case "UP": return 1;
    case "FLAT": return 0;
    case "DOWN": return -1;
    default: return null;
  }
}

/** League interest as an orderable number. The labels are coarse behavioural buckets, not forecasts. */
export function competitionRank(interest: string | null | undefined): number | null {
  switch (interest) {
    case "LOW": return 1;
    case "MEDIUM": return 2;
    case "HIGH": return 3;
    default: return null;
  }
}

type Rankable = {
  recommendation: string;
  components: { decision?: { actionable?: boolean } | null };
};

export type TopClaims<T> =
  /** Moves the analysis calls actionable, in the backend's ranking order. */
  | { mode: "actionable"; rows: T[] }
  /** Nothing is actionable; the best-ranked options that are not an outright pass. */
  | { mode: "watch"; rows: T[] }
  | { mode: "none"; rows: T[] };

/**
 * The band at the top of the page. `rows` must already be in the backend's ranking order (actionable
 * first, then by roster utility). Actionable moves win; if there are none, fall back to the best
 * ranked rows that are not marked IGNORE, so the band never presents a pass as a claim.
 */
export function pickTopClaims<T extends Rankable>(rows: readonly T[], n = 3): TopClaims<T> {
  const actionable = rows.filter((r) => r.components.decision?.actionable === true);
  if (actionable.length > 0) return { mode: "actionable", rows: actionable.slice(0, n) };
  const watch = rows.filter((r) => r.recommendation !== "IGNORE");
  if (watch.length > 0) return { mode: "watch", rows: watch.slice(0, n) };
  return { mode: "none", rows: [] };
}

type Params = Record<string, string | string[] | undefined>;

/** `?query` for the current search params with `updates` applied; a null value removes the param. */
export function withParams(sp: Params, updates: Record<string, string | null>): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(sp)) {
    if (k in updates || v === undefined) continue;
    for (const item of Array.isArray(v) ? v : [v]) qs.append(k, item);
  }
  for (const [k, v] of Object.entries(updates)) if (v) qs.set(k, v);
  const s = qs.toString();
  return s ? `?${s}` : "?";
}

type ScoreRow = {
  waiver_score: number | null;
  components?: { roster?: { fit_status?: string; value_unknown?: { name: string | null }[] } | null } | null;
};

/** The waiver score as a measured value, or null when it lacks a term: an unknown lineup gain (a player without a
 * historical value) is `roster.fit_status` "partial", and the score must not read as if that term were 0. */
export function knownWaiverScore(row: ScoreRow): number | null {
  return row.components?.roster?.fit_status === "partial" ? null : row.waiver_score;
}

/** Why a waiver score is not shown (for the Missing tooltip). */
export function waiverScoreGap(row: ScoreRow): string {
  const names = (row.components?.roster?.value_unknown ?? []).map((u) => u.name).filter(Boolean);
  return `Partial score: lineup gain unknown${names.length ? ` (no historical value for ${names.join(", ")})` : ""}; unknown is not zero.`;
}

/** A stored positional need computed without its starter-quality term (`details.value_status` "partial"). */
export function needPartialReason(details: { value_status?: string; value_note?: string | null } | null | undefined): string | null {
  if (details?.value_status !== "partial") return null;
  return details.value_note ?? "Starter quality unknown; this need uses roster shortage only.";
}
