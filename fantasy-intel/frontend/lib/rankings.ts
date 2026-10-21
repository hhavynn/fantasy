// Pure helpers for the /rankings page: URL state, the API path, labels and number formatting.
// No React and no `@/` imports, so they run under the unit-test runner (node --experimental-strip-types);
// like the other tested lib files, only type-only imports reach into sibling modules (`./sort.ts`).
// The contract (methodology, API, response shape) is docs/player_rankings.md.
import type { SearchParams, SortState } from "./sort.ts";
import type { ProjectionSource } from "./projection-source.ts";

export type RankPosition = "ALL" | "QB" | "RB" | "WR" | "TE";
export type RankHorizon = "week" | "ros";
export type RosterFilter = "all" | "rostered" | "available";

export const RANK_POSITIONS: readonly RankPosition[] = ["ALL", "QB", "RB", "WR", "TE"];
/** Sort keys the API accepts (`GET /api/rankings?sort=`). */
export const RANKING_SORT_KEYS: readonly string[] = [
  "rank", "name", "espn", "ours", "rating", "opportunity", "production", "xfp", "ppr", "consistency", "trend",
];
export const ROSTER_FILTERS: readonly RosterFilter[] = ["all", "rostered", "available"];
export const RANK_HORIZONS: readonly RankHorizon[] = ["week", "ros"];
/** Rows per page; also the API's default `limit`. */
export const PAGE_SIZE = 100;

/** The longest name search the page sends. */
const MAX_QUERY = 60;
/** Sorts that start ascending when no direction is given (best first / A to Z); every other column starts high to low. */
const ASC_FIRST = new Set(["rank", "name", "consistency"]);   // lower SD = steadier

export type RankingsFilters = {
  pos: RankPosition;
  horizon: RankHorizon;
  source: ProjectionSource;
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
 * Read the page's filters from the URL. Anything missing or invalid becomes the default (ALL, this week,
 * any roster status, no search, rank order, first page). `source=ours|espn` in the URL wins over the
 * header switch's cookie; without it the cookie decides.
 *
 * Sorting follows `parseSort` in lib/sort.ts (an unknown key falls back to rank ascending, as does an invalid
 * direction on the rank column) with one difference that matches the API: a column given without a valid
 * `dir` starts in its natural direction, so `sort=rating` means highest first. It is written out here because
 * the unit-test runner cannot load a runtime import of ./sort.ts under the project's TypeScript settings.
 *
 * A league whose provider publishes no projections (`providerProjections` false, Sleeper) is always on Ours: a
 * `source=espn` link is ignored there, so the page never asks the API for ESPN numbers it does not have.
 */
export function parseRankingsFilters(sp: SearchParams | undefined, cookieSource?: ProjectionSource, providerProjections = true): RankingsFilters {
  const urlSource = first(sp?.source);
  const sortKey = first(sp?.sort);
  const dirParam = first(sp?.dir);
  const offsetParam = first(sp?.offset);

  const key = sortKey !== undefined && RANKING_SORT_KEYS.includes(sortKey) ? sortKey : "rank";
  const naturalDir = ASC_FIRST.has(key) ? "asc" : "desc";
  const dir = dirParam === "asc" || dirParam === "desc" ? dirParam : naturalDir;
  const offset = offsetParam !== undefined && /^\d+$/.test(offsetParam.trim()) ? Number(offsetParam.trim()) : 0;

  return {
    pos: oneOf(first(sp?.pos), RANK_POSITIONS, "ALL"),
    horizon: oneOf(first(sp?.horizon), RANK_HORIZONS, "week"),
    source: !providerProjections ? "ours"
      : urlSource === "ours" || urlSource === "espn" ? urlSource : cookieSource === "ours" ? "ours" : "espn",
    status: oneOf(first(sp?.status), ROSTER_FILTERS, "all"),
    q: (first(sp?.q) ?? "").trim().slice(0, MAX_QUERY),
    sort: { key, dir },
    offset: Number.isSafeInteger(offset) ? offset : 0,
  };
}

/**
 * The backend path for a set of filters. "Ours" is the API's `independent` source; `q` is sent only when
 * there is a search; everything is encoded by URLSearchParams.
 */
export function rankingsApiPath(f: RankingsFilters, limit: number = PAGE_SIZE): string {
  const qs = new URLSearchParams({
    position: f.pos,
    horizon: f.horizon,
    source: f.source === "ours" ? "independent" : "espn",
    sort: f.sort.key,
    dir: f.sort.dir,
    status: f.status,
    limit: String(limit),
    offset: String(f.offset),
  });
  if (f.q) qs.set("q", f.q);
  return `/api/rankings?${qs.toString()}`;
}

/**
 * `?query` for a link to this page that keeps the current search params and applies `patch`: a string sets
 * the param, `null` removes it. Changing any param other than `offset` also drops the page offset (a new
 * filter or sort starts again at page one) unless the patch itself sets an `offset`. An empty result is
 * `"?"`, which links back to the page with no query.
 */
export function rankingsHref(sp: SearchParams | undefined, patch: Record<string, string | null>): string {
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

// ----------------------------------------------------------------------------- formatting

const DASH = "—";
const finite = (v: number | null | undefined): v is number => typeof v === "number" && Number.isFinite(v);

/** A 0–100 rating with one decimal, always ("87.5", "100.0", "0.0"); out-of-range values are clamped and a missing one is a dash, never 0.0. */
export function formatRating(v: number | null | undefined): string {
  return finite(v) ? Math.min(100, Math.max(0, v)).toFixed(1) : DASH;
}

/** Fantasy points with one decimal; a missing value is a dash and a real zero stays "0.0". */
export function formatPoints(v: number | null | undefined): string {
  return finite(v) ? v.toFixed(1) : DASH;
}

// ----------------------------------------------------------------------------- API types (docs/player_rankings.md)

export type RankingWindow = {
  weeks: number[]; first: number | null; last: number | null; label: string; regular_season_only: boolean;
  last_regular_season_week: number | null; note: string | null;
};

export type OursProjectionInfo = {
  available: boolean; reason: string | null; model_version: string | null; model_versions?: string[];
  run_ids?: string[]; run_created_at: string | null; experimental: boolean; label: string | null; governance_note: string | null;
};

export type RankingRow = {
  espn_player_id: number;
  gsis_id: string | null;
  full_name: string | null;
  position: string | null;
  pro_team: string | null;
  rank: { overall: number | null; position: number | null; position_label: string | null; tied: boolean };
  projection: {
    value: number | null; espn: number | null; ours: number | null; ours_origin: string | null;
    /** ok | bye | missing | incomplete | no_ours */
    status: string;
    weeks_expected: number; weeks_available: number; missing_weeks: number[]; bye_weeks: number[];
  };
  rating: { overall: number | null; opportunity: number | null; production: number | null };
  opportunity: { basis: string | null; xfp_per_game: number | null; recent_per_game: number | null; tier: string | null };
  production: {
    ppr_per_game: number | null; recent_ppr_per_game: number | null; fp_minus_xfp: number | null; sustainability: string | null;
  };
  consistency: { ppr_sd: number | null; games: number | null };
  games: number | null;
  role_trend: string | null;
  usage_trend_score: number | null;
  market_signal: string | null;
  availability: {
    /** AVAILABLE | QUESTIONABLE | DOUBTFUL | OUT | IR | SUSPENDED */
    status: string; weight: number; sources: string[]; espn_injury_status: string | null; nfl_report_status: string | null;
  };
  game: { week: number | null; opponent: string | null; is_home: boolean | null; on_bye: boolean | null };
  roster: {
    /** ROSTERED | FREE_AGENT | WAIVERS | UNKNOWN */
    status: string; fantasy_team_id: number | null; fantasy_team: string | null; is_mine: boolean;
  };
  why: string[];
};

export type RankingsResponse = {
  season: number;
  week: number;
  horizon: RankHorizon;
  source: string;
  position: RankPosition;
  sort: SortState;
  window: RankingWindow;
  projection: {
    /** What ranks: ESPN's number or Fantasy Intel's. */
    basis: "espn" | "ours";
    label: string;
    espn: { synced_at: string | null; label: string | null };
    ours: OursProjectionInfo | null;
  };
  rating: {
    scale: string; decimals: number; contract: string; adapter: string; semantics: string;
    as_of_week: number | null; computed_at: string | null;
  };
  scoring: { projection_units: string; rating_units: string; ours_supported: boolean; ours_reason: string | null };
  league_context: { available: boolean; league_name: string | null; teams: number | null; my_team_id: number | null };
  freshness: {
    espn_projections_synced_at: string | null; player_scores_computed_at: string | null;
    player_scores_as_of_week: number | null; players_synced_at: string | null; ours_run_created_at: string | null;
  };
  counts: {
    pool: number; ranked: number;
    unranked: { missing: number; bye: number; incomplete: number; no_ours: number };
    /** After the position, search and roster filters. */
    matched: number;
    returned: number;
  };
  notes: string[];
  players: RankingRow[];
};

// ----------------------------------------------------------------------------- labels

/** Why a player has no rank (RankingRow.projection.status). */
export function unrankedLabel(status: string | null | undefined): string {
  switch (status) {
    case "bye": return "Bye";
    case "missing": return "No projection";
    case "incomplete": return "Incomplete";
    case "no_ours": return "No Ours estimate";
    default: return "Unranked";
  }
}

/** "WR3", "T-WR3" when tied, or the reason an unranked player has no rank. */
export function rankLabel(row: Pick<RankingRow, "rank" | "projection">): string {
  const { overall, position_label, tied } = row.rank;
  if (overall === null || overall === undefined) return unrankedLabel(row.projection?.status);
  return `${tied ? "T-" : ""}${position_label ?? `#${overall}`}`;
}

const AVAILABILITY_WARNING: Record<string, string> = {
  OUT: "Out", IR: "Injured reserve", SUSPENDED: "Suspended", DOUBTFUL: "Doubtful", QUESTIONABLE: "Questionable",
  // The raw provider spellings (ESPN's, which Sleeper statuses are mapped to) mean the same.
  INJURY_RESERVE: "Injured reserve", SUSPENSION: "Suspended",
};

/** The one-line warning for a player, or null when he is healthy and plays. A bye outranks an injury. */
export function availabilityWarning(row: Pick<RankingRow, "availability" | "game">): string | null {
  if (row.game?.on_bye) return "Bye week";
  return AVAILABILITY_WARNING[row.availability?.status] ?? null;
}

export type RosterTone = "accent" | "good" | "muted" | "warn";

/** Who has the player in this league, as a badge text and a tone the component maps to a Badge tone. */
export function rosterLabel(roster: RankingRow["roster"]): { text: string; tone: RosterTone } {
  if (roster.is_mine) return { text: "My team", tone: "accent" };
  switch (roster.status) {
    case "ROSTERED": return { text: roster.fantasy_team ?? "Rostered", tone: "muted" };
    case "FREE_AGENT": return { text: "Free agent", tone: "good" };
    case "WAIVERS": return { text: "Waivers", tone: "warn" };
    default: return { text: "Unknown", tone: "muted" };
  }
}

/** The player profile for an ESPN player id. */
export function playerHref(id: number): string {
  return `/players?id=${id}`;
}

/** "vs BUF" (home), "@ BUF" (away), "BYE", or a dash when the opponent is unknown. */
export function opponentLabel(game: RankingRow["game"]): string {
  if (game.on_bye) return "BYE";
  if (!game.opponent) return DASH;
  if (game.is_home === true) return `vs ${game.opponent}`;
  if (game.is_home === false) return `@ ${game.opponent}`;
  return game.opponent;
}

/** Role trend as an orderable number: rising 2, flat 1, falling 0; insufficient or unknown has none. */
export function trendRankValue(t: string | null | undefined): number | null {
  switch (t) {
    case "UP": return 2;
    case "FLAT": return 1;
    case "DOWN": return 0;
    default: return null;
  }
}
