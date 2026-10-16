// URL-backed table sorting. Server components read `?sort=<key>&dir=asc|desc`, sort the rows
// on the server and render `SortableTh` links; no client state.

export type SortDir = "asc" | "desc";
export type SortState = { key: string; dir: SortDir };
export type SearchParams = Record<string, string | string[] | undefined>;

/** Query-string names for a sortable table. The default table uses `sort` + `dir`; a second table
 *  on the same page passes its own `param` (e.g. "moves" → `moves` + `moves_dir`). */
export function sortParamNames(param = "sort"): { key: string; dir: string } {
  return { key: param, dir: param === "sort" ? "dir" : `${param}_dir` };
}

const first = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

/**
 * Read the sort state from search params. An unknown or missing key falls back to `fallback`
 * (the table's default order). A missing or invalid direction uses the fallback's direction when
 * the key is the fallback key, otherwise `asc`.
 */
export function parseSort(
  sp: SearchParams | undefined,
  allowed: readonly string[],
  fallback: SortState,
  param = "sort",
): SortState {
  const names = sortParamNames(param);
  const key = first(sp?.[names.key]);
  if (!key || !allowed.includes(key)) return { key: fallback.key, dir: fallback.dir };
  const dir = first(sp?.[names.dir]);
  if (dir === "asc" || dir === "desc") return { key, dir };
  return { key, dir: key === fallback.key ? fallback.dir : "asc" };
}

type Sortable = number | string | null | undefined;

const isMissing = (v: Sortable): v is null | undefined =>
  v === null || v === undefined || (typeof v === "number" && Number.isNaN(v));

function compare(a: number | string, b: number | string): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "string" && typeof b === "string") return a.localeCompare(b);
  return typeof a === "number" ? -1 : 1; // mixed column: numbers sort before strings (ascending)
}

/**
 * Sorted copy of `rows`. Stable (ties keep their original order), and missing values
 * (null, undefined, NaN) are always last in both directions: a missing number is not zero.
 */
export function sortRows<T>(rows: readonly T[], accessor: (row: T) => Sortable, dir: SortDir): T[] {
  const sign = dir === "asc" ? 1 : -1;
  return rows
    .map((row, index) => ({ row, index, value: accessor(row) }))
    .sort((a, b) => {
      const am = isMissing(a.value);
      const bm = isMissing(b.value);
      if (am || bm) return am && bm ? a.index - b.index : am ? 1 : -1;
      return sign * compare(a.value as number | string, b.value as number | string) || a.index - b.index;
    })
    .map((x) => x.row);
}

/** `?query` for the link that sorts by `key`, preserving every other search param. */
export function sortHref(sp: SearchParams | undefined, key: string, dir: SortDir, param = "sort"): string {
  const names = sortParamNames(param);
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(sp ?? {})) {
    if (k === names.key || k === names.dir || v === undefined) continue;
    for (const item of Array.isArray(v) ? v : [v]) qs.append(k, item);
  }
  qs.set(names.key, key);
  qs.set(names.dir, dir);
  return `?${qs.toString()}`;
}

/** Direction a click on column `key` requests: flip when it is already active, else the column's default. */
export function nextDir(current: SortState, key: string, defaultDir: SortDir): SortDir {
  if (current.key !== key) return defaultDir;
  return current.dir === "asc" ? "desc" : "asc";
}
