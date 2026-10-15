// Pure helpers for the trade discovery table: which sort keys exist, how the URL's sort maps onto the API's
// `sort` parameter, and the value each column sorts by. Unit-tested in lib/trade-discovery.test.ts.

import type { SearchParams, SortState } from "./sort.ts";

/** Sort values the API accepts (`GET /api/trades/discover?sort=`). In "All candidates" mode they also decide
 *  which rows are returned when more candidates exist than `limit`. */
export const API_SORTS = ["my_gain", "their_gain", "combined", "min_side", "playoff_gain", "min_side_regular", "min_side_playoff"] as const;

/** Every key the table can sort by: the API's own plus columns that only reorder the rows already returned. */
export const TABLE_SORTS = ["rank", "shape", "partner", "asset", ...API_SORTS] as const;

export const isApiSort = (v: string | undefined): v is (typeof API_SORTS)[number] => !!v && (API_SORTS as readonly string[]).includes(v);

/** Minimal row shape the sort needs (a `DResult` satisfies it). */
export type SortableTrade = {
  shape: string;
  opponent_name: string | null;
  mine: { ros_delta: number | null; regular_season_delta: number | null; playoff_delta: number | null };
  theirs: { ros_delta: number | null; regular_season_delta: number | null; playoff_delta: number | null };
  combined_ros_delta: number | null;
  min_side_ros_delta: number | null;
  asset_balance: { ratio: number | null };
};

const minOf = (a: number | null, b: number | null) => (a === null || b === null ? null : Math.min(a, b));

/** The value a column sorts by; `null` sorts last in either direction (missing is never treated as 0). */
export function sortValue(r: SortableTrade, key: string, rank: number): number | string | null {
  switch (key) {
    case "shape": return r.shape;
    case "partner": return r.opponent_name;
    case "asset": return r.asset_balance.ratio;
    case "my_gain": return r.mine.ros_delta;
    case "their_gain": return r.theirs.ros_delta;
    case "combined": return r.combined_ros_delta;
    case "min_side": return r.min_side_ros_delta;
    case "playoff_gain": return r.mine.playoff_delta;
    case "min_side_regular": return minOf(r.mine.regular_season_delta, r.theirs.regular_season_delta);
    case "min_side_playoff": return minOf(r.mine.playoff_delta, r.theirs.playoff_delta);
    default: return rank;
  }
}

/** The order the API returns rows in: its own tier/ranking order, except "All candidates" which is sorted by gain. */
export function defaultSort(mode: string): SortState {
  return mode === "all" ? { key: "my_gain", dir: "desc" } : { key: "rank", dir: "asc" };
}

const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

/**
 * Sort state from the URL (`?sort=<key>&dir=asc|desc`; same rules as `parseSort` in lib/sort.ts). An unknown key falls
 * back to the mode's default order. A bare `?sort=<api sort>` (an older link, no `dir`) keeps meaning "best first", so it
 * resolves to descending; other keys without a direction ascend.
 */
export function resolveSort(sp: SearchParams | undefined, mode: string): SortState {
  const fallback = defaultSort(mode);
  const key = first(sp?.sort);
  if (!key || !(TABLE_SORTS as readonly string[]).includes(key)) return fallback;
  const dir = first(sp?.dir);
  if (dir === "asc" || dir === "desc") return { key, dir };
  if (key === fallback.key) return fallback;
  return { key, dir: isApiSort(key) ? "desc" : "asc" };
}
