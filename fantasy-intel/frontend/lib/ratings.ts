// Versioned 0–100.0 player ratings (docs/player_ratings_100.md). Pure helpers, no React.
// A rating is a relative, position-relative rank; the legacy 0–10 fields stay the backend's policy
// inputs and are only ever shown as an explicitly labelled legacy fallback, never rescaled.

export const RATING_SCALE_VERSION = "ratings-100.v1";

export type RatingKind = "opportunity" | "production" | "value";

/** A rating on the 0–100 scale, or a legacy 0–10 value that must be labelled as such. */
export type Rating = { scale: 100; value: number } | { scale: 10; value: number; legacy: true };

/** Fields read from a player row (`PlayerCore`) or a shape built to look like one (the waiver row). The
 *  `_100` fields are absent on a hosted database written before the 0–100 scale, so all are optional. */
export type RatingSource = {
  opportunity_score_100?: number | null; opportunity_score?: number | null;
  production_score_100?: number | null; production_score?: number | null;
  value_score_100?: number | null; value_score?: number | null;
};

const FIELDS = {
  opportunity: ["opportunity_score_100", "opportunity_score"],
  production: ["production_score_100", "production_score"],
  value: ["value_score_100", "value_score"],
} as const;

export const RATING_LABEL: Record<RatingKind, string> = {
  opportunity: "Opportunity", production: "Production", value: "Value",
};

/** What each rating ranks, for tooltips and aria text. */
export const RATING_DESCRIPTION: Record<RatingKind, string> = {
  opportunity: "position percentile of recent opportunity per game (expected fantasy points, or targets plus carries)",
  production: "position percentile of recent PPR points per game",
  value: "heuristic blend of 0.75 opportunity, 0.25 production and the role trend",
};

/** The caveat that goes with every 0–100 rating. */
export const RATING_CAVEAT =
  "Ratings are relative ranks among players with enough games, not probabilities, not calibrated projected fantasy points and not trade prices. 100 is the top of a relative scale.";

/** Why a missing or legacy-only rating is not shown on the 0–100 scale. */
export const RATING_PENDING = "The 0–100 rating has not been computed yet.";

const present = (v: number | null | undefined): v is number => typeof v === "number" && Number.isFinite(v);

/** The 0–100 rating if present; otherwise the legacy 0–10 value flagged `legacy`; otherwise null. */
export function ratingFor(source: RatingSource | null | undefined, kind: RatingKind): Rating | null {
  if (!source) return null;
  const [field100, legacyField] = FIELDS[kind];
  const v100 = source[field100];
  if (present(v100)) return { scale: 100, value: v100 };
  const legacy = source[legacyField];
  return present(legacy) ? { scale: 10, value: legacy, legacy: true } : null;
}

/** "87.4/100", or "8.7/10 (legacy)" for a legacy-only value, or null when there is nothing to show. */
export function formatRating(r: Rating | null | undefined): string | null {
  if (!r) return null;
  return r.scale === 100 ? `${r.value.toFixed(1)}/100` : `${r.value.toFixed(1)}/10 (legacy)`;
}

/** The sortable number: only a 0–100 value. Legacy-only and missing are null so they never mix scales. */
export function ratingSortKey(r: Rating | null | undefined): number | null {
  return r && r.scale === 100 ? r.value : null;
}

/** Comparator for `Array.sort`: ascending or descending by 0–100 value, with missing and legacy-only last either way. */
export function compareRatings(dir: "asc" | "desc" = "desc") {
  return (a: Rating | null | undefined, b: Rating | null | undefined): number => {
    const x = ratingSortKey(a);
    const y = ratingSortKey(b);
    if (x === null || y === null) return x === y ? 0 : x === null ? 1 : -1;
    return dir === "asc" ? x - y : y - x;
  };
}
