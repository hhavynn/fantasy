// Pure helpers for the projection-source comparison (/rankings/compare): URL state, the API path, labels and text.
// No React and no `@/` imports, so they run under the unit-test runner; only type-only imports reach sibling modules.
// The contract is GET /api/rankings/compare (`projection-compare/v1`, backend app/analytics/projection_compare.py).
import type { SearchParams, SortState } from "./sort.ts";
import type { RankingRow, RankingWindow, RankHorizon, RankPosition, RosterFilter } from "./rankings.ts";

export type CompareScope = "position" | "overall";
export type CompareShow = "all" | "large" | "compared" | "missing";

export const COMPARE_POSITIONS: readonly RankPosition[] = ["ALL", "QB", "RB", "WR", "TE"];
export const COMPARE_HORIZONS: readonly RankHorizon[] = ["week", "ros"];
export const COMPARE_SCOPES: readonly CompareScope[] = ["position", "overall"];
export const COMPARE_SHOW: readonly CompareShow[] = ["all", "large", "compared", "missing"];
export const COMPARE_ROSTER: readonly RosterFilter[] = ["all", "rostered", "available"];
/** Sort keys the API accepts (`GET /api/rankings/compare?sort=`). */
export const COMPARE_SORT_KEYS: readonly string[] = ["disagreement", "delta", "points", "provider_rank", "ours_rank", "name"];
export const COMPARE_PAGE_SIZE = 100;
const ASC_FIRST = new Set(["provider_rank", "ours_rank", "name"]);
const MAX_QUERY = 60;

export type CompareFilters = {
  pos: RankPosition;
  horizon: RankHorizon;
  scope: CompareScope;
  show: CompareShow;
  status: RosterFilter;
  q: string;
  sort: SortState;
  offset: number;
};

const first = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

function oneOf<T extends string>(value: string | undefined, allowed: readonly T[], fallback: T): T {
  return value !== undefined && (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
}

/**
 * The page's filters from the URL; anything missing or invalid is the default (all positions, this week, ranks within
 * the position, every player, largest disagreement first, first page). A sort given without a valid `dir` starts in
 * its natural direction (ranks and names ascending, deltas descending).
 */
export function parseCompareFilters(sp: SearchParams | undefined): CompareFilters {
  const sortKey = first(sp?.sort);
  const key = sortKey !== undefined && COMPARE_SORT_KEYS.includes(sortKey) ? sortKey : "disagreement";
  const dirParam = first(sp?.dir);
  const dir = dirParam === "asc" || dirParam === "desc" ? dirParam : ASC_FIRST.has(key) ? "asc" : "desc";
  const offsetParam = first(sp?.offset)?.trim();
  const offset = offsetParam !== undefined && /^\d+$/.test(offsetParam) ? Number(offsetParam) : 0;
  return {
    pos: oneOf(first(sp?.pos), COMPARE_POSITIONS, "ALL"),
    horizon: oneOf(first(sp?.horizon), COMPARE_HORIZONS, "week"),
    scope: oneOf(first(sp?.scope), COMPARE_SCOPES, "position"),
    show: oneOf(first(sp?.show), COMPARE_SHOW, "all"),
    status: oneOf(first(sp?.status), COMPARE_ROSTER, "all"),
    q: (first(sp?.q) ?? "").trim().slice(0, MAX_QUERY),
    sort: { key, dir },
    offset: Number.isSafeInteger(offset) ? offset : 0,
  };
}

/** The backend path for a set of filters; every filter is sent, so the response (and its cache key) follows the URL. */
export function compareApiPath(f: CompareFilters, limit: number = COMPARE_PAGE_SIZE): string {
  const qs = new URLSearchParams({
    position: f.pos, horizon: f.horizon, scope: f.scope, show: f.show, status: f.status,
    sort: f.sort.key, dir: f.sort.dir, limit: String(limit), offset: String(f.offset),
  });
  if (f.q) qs.set("q", f.q);
  return `/api/rankings/compare?${qs.toString()}`;
}

/**
 * `?query` for a link to this page that keeps the current params and applies `patch` (a string sets, `null` removes).
 * Changing anything but `offset` starts again at page one.
 */
export function compareHref(sp: SearchParams | undefined, patch: Record<string, string | null>): string {
  const resetsPaging = Object.keys(patch).some((k) => k !== "offset");
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(sp ?? {})) {
    if (k in patch || v === undefined) continue;
    if (k === "offset" && resetsPaging) continue;
    for (const item of Array.isArray(v) ? v : [v]) qs.append(k, item);
  }
  for (const [k, v] of Object.entries(patch)) if (v !== null) qs.set(k, v);
  return `?${qs.toString()}`;
}

// ----------------------------------------------------------------------------- API types

export type CompareRank = {
  overall: number | null; overall_tied: boolean; position: number | null; position_tied: boolean; position_label: string | null;
};
/** ok | bye | missing | incomplete | not_published (provider); ok | bye | no_estimate | unavailable (Ours). */
export type CompareSide = { value: number | null; status: string; reason: string | null; rank: CompareRank; origin?: string | null };
/** ok | provider_missing | ours_missing | neither_ranked | provider_not_published | ours_unavailable */
export type CompareDelta = {
  status: string; rank: number | null; points: number | null; points_per_week: number | null;
  direction: "ours_higher" | "provider_higher" | "same" | null; tied: boolean; relevant: boolean; large: boolean;
};
export type CompareRange =
  | { available: false; reason: string | null }
  | {
    available: true; floor: number; median: number; ceiling: number; centre: number; week: number; horizon: string; basis: string;
    coverage: { nominal: number; text: string; confirmation: { window: string; rows: number; coverage: number } | null;
      replay: { window: string; coverage: number } | null };
    sample: { games_used: number | null; latest_usage: { season: number; week: number } | null };
  };
export type ModelInput = { feature: string | null; text: string | null; contribution: number | null; value: unknown; training_mean: unknown };
export type ModelDrivers =
  | { available: false; reason: string }
  | {
    available: true; week: number; inputs: ModelInput[]; inputs_recorded: number; flags: string[]; missing_inputs: string[];
    coverage_status: string | null; p_play: number | null; participation_status: string | null; games_used: number | null;
    latest_usage: { season: number; week: number } | null;
    run: { run_id: string; model_version: string; created_at: string | null; data_through: { season: number; week: number } | null;
      forecast_horizon: number | null; artifact_check: string | null };
    note: string;
  };
export type HistorySignal = { kind: string; label: string; before: number | string | null; after: number | string | null;
  unit: string; source: string | null; detail: string | null };
export type HistoryPlayer =
  | { listed: false; text: string }
  | { listed: true; before: number; after: number; delta: number; rank_before: number | null; rank_after: number | null;
    direction: string; evidence: { headline: string | null; signals: HistorySignal[]; missing: string[]; note: string | null } };
export type HistoryBlock = { label: string; available: boolean; reason: string | null; baseline: string | null; current: string | null;
  weeks: number[] | null; player: HistoryPlayer | null };
export type CompareExplanation = { model: ModelDrivers; history: { ours: HistoryBlock; provider: HistoryBlock } };

export type CompareRow = {
  espn_player_id: number; gsis_id: string | null; full_name: string | null; position: string | null; pro_team: string | null;
  availability: RankingRow["availability"]; game: RankingRow["game"]; roster: RankingRow["roster"]; bye_weeks: number[];
  sources: { provider: CompareSide; ours: CompareSide };
  delta: CompareDelta; range: CompareRange | null; explanation: CompareExplanation | null;
};

export type CompareSource = {
  key: "provider" | "ours"; kind: "provider" | "model"; label: string; eligible: boolean; available: boolean;
  reason: string | null; ranked: number; description: string | null; provider_name?: string; model_version?: string | null;
  run_created_at?: string | null; synced_at?: string | null; experimental?: boolean; governance_note?: string | null; note?: string | null;
};

export type CompareResponse = {
  contract: string; season: number; week: number; horizon: RankHorizon; position: RankPosition; scope: CompareScope;
  show: CompareShow; sort: SortState; window: RankingWindow; rosters_synced: boolean;
  provider: { key: string; name: string; publishes_projections: boolean };
  sources: CompareSource[];
  thresholds: { large_rank: number; min_points_per_week: number; relevant_per_week: number; explain_max: number };
  ranges: {
    available: boolean; reason: string | null; version: string; coverage: number; definition: string; week: number; horizon: string;
    validation_source: string; rest_of_season: { available: boolean; reason: string };
    positions: Record<string, { served: boolean; reason: string | null }>;
  };
  counts: {
    pool: number; matched: number; shown: number; returned: number; compared: number; large: number; ours_higher: number;
    provider_higher: number; provider_missing: number; ours_missing: number; neither_ranked: number;
  };
  largest: { espn_player_id: number; full_name: string | null; position: string | null; rank: number; points: number; direction: string }[];
  notes: string[];
  players: CompareRow[];
};

// ----------------------------------------------------------------------------- labels

/** The rank in the chosen scope as text ("WR3", "T-WR3", "#12", "T-#12"), or null when the side has no rank. */
export function sideRankText(side: CompareSide, scope: CompareScope): string | null {
  const r = side.rank;
  if (scope === "position") {
    if (r.position === null || r.position === undefined) return null;
    return `${r.position_tied ? "T-" : ""}${r.position_label ?? `#${r.position}`}`;
  }
  if (r.overall === null || r.overall === undefined) return null;
  return `${r.overall_tied ? "T-" : ""}#${r.overall}`;
}

/** Why a side has no rank, in a few words: never a number. */
export function sideMissingText(side: CompareSide): string {
  switch (side.status) {
    case "bye": return "Bye";
    case "missing": return "No projection";
    case "incomplete": return "Incomplete";
    case "no_estimate": return "No estimate";
    case "not_published": return "Not published";
    case "unavailable": return "Unavailable";
    default: return "Unranked";
  }
}

/** The delta column's text for a row without a delta: which side is missing. Null for a row that has one. */
export function deltaMissingText(d: CompareDelta, providerName: string): string | null {
  switch (d.status) {
    case "ok": return null;
    case "provider_missing": return `No ${providerName} rank`;
    case "ours_missing": return "No Ours rank";
    case "neither_ranked": return "Neither ranked";
    case "provider_not_published": return `${providerName} publishes none`;
    case "ours_unavailable": return "Ours unavailable";
    default: return "Not compared";
  }
}

/** "Ours ranks him 7 places higher than ESPN" for a row with a delta; null otherwise. A zero says both agree. */
export function deltaSentence(d: CompareDelta, providerName: string, scope: CompareScope): string | null {
  if (d.status !== "ok" || d.rank === null) return null;
  const where = scope === "position" ? "within his position" : "overall";
  const n = Math.abs(d.rank);
  const places = `${n} ${n === 1 ? "place" : "places"}`;
  if (d.rank === 0) return `Same rank ${where} from both sources${d.tied ? " (a tie)" : ""}`;
  return d.rank > 0
    ? `Ours ranks him ${places} higher than ${providerName} ${where}`
    : `${providerName} ranks him ${places} higher than Ours ${where}`;
}

/** "8.1–33.0" for an available range. */
export function rangeText(r: CompareRange | null): string | null {
  return r && r.available ? `${r.floor.toFixed(1)}–${r.ceiling.toFixed(1)}` : null;
}

/** "Week 7 only" when the page shows rest of season, so a next-week range is never read as a season range. */
export function rangeHorizonNote(horizon: RankHorizon, week: number): string {
  return horizon === "ros" ? `Week ${week} only; no rest-of-season range` : `Week ${week}`;
}

/** A model input's contribution in points with a sign, or null when the run records none. */
export function contributionText(c: number | null | undefined): string | null {
  if (c === null || c === undefined || Number.isNaN(c)) return null;
  const v = Math.abs(c).toFixed(1);
  return Number(v) === 0 ? "0.0" : `${c > 0 ? "+" : "−"}${v}`;
}

/**
 * The verdict's figures (compared, large gaps, direction totals) are counted over every player matching the position,
 * roster and name filters, before the Show filter narrows the table. Their notes say so whenever Show is narrowing, so
 * a figure is never read as a count of the rows listed below it.
 */
export function verdictNotes(show: CompareShow, matched: number): { compared: string; large: string; scope: string | null } {
  const narrowed = show !== "all";
  return {
    compared: narrowed ? `of ${matched} before the Show filter` : `of ${matched} in view`,
    large: narrowed ? "large gaps, before the Show filter" : "large gaps",
    scope: narrowed
      ? `These counts cover all ${matched} players matching the position, roster and name filters, before the Show filter (${SHOW_LABEL[show]}).`
      : null,
  };
}

export const SCOPE_LABEL: Record<CompareScope, string> = { position: "Within position", overall: "All positions" };
export const SHOW_LABEL: Record<CompareShow, string> = {
  all: "All players", large: "Large gaps", compared: "Both ranked", missing: "One side missing",
};
