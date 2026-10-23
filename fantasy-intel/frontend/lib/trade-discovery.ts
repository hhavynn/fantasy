// Pure helpers for the trade discovery table: which sort keys exist, how the URL's sort maps onto the API's
// `sort` parameter, and the value each column sorts by. Unit-tested in lib/trade-discovery.test.ts.

import type { SearchParams, SortState } from "./sort.ts";
import type { BeamWorkBudget } from "./trade-types.ts";

// Lib modules import each other only for types (node's test runner and tsc disagree about ".ts" import paths), so these two
// constants repeat lib/trade-dual-value.ts's `DEFAULT_MODE` and `VERDICT_ORDER`; lib/trade-dual-value.test.ts asserts they agree.
/** The page's default lens: a URL without `mode` sends it explicitly (the API's own default stays `practical`). */
export const PAGE_DEFAULT_MODE = "mutual_upgrades";
/** The backend's verdict order (`trade_dual_value.VERDICTS`), best first. */
export const VERDICT_RANK_ORDER = ["sensible", "sensible_with_concerns", "unlikely_mutual", "fair_no_lineup_gain", "one_sided", "incomplete"] as const;
const verdictRank = (v: string | null | undefined): number | null => {
  const i = (VERDICT_RANK_ORDER as readonly string[]).indexOf(v ?? "");
  return i < 0 ? null : i;
};

/** Sort values the API accepts (`GET /api/trades/discover?sort=`). In "All candidates" mode they also decide
 *  which rows are returned when more candidates exist than `limit`. */
export const API_SORTS = ["my_gain", "their_gain", "combined", "min_side", "playoff_gain", "min_side_regular", "min_side_playoff"] as const;

/** Every key the table can sort by: the API's own plus columns that only reorder the rows already returned. */
export const TABLE_SORTS = ["rank", "shape", "partner", "asset", "verdict", ...API_SORTS] as const;

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
  /** Present on dual-value rows; `verdict` is one of the backend's verdict codes. */
  dual_value_summary?: { verdict: string } | null;
};

const minOf = (a: number | null, b: number | null) => (a === null || b === null ? null : Math.min(a, b));

/** The value a column sorts by; `null` sorts last in either direction (missing is never treated as 0). */
export function sortValue(r: SortableTrade, key: string, rank: number): number | string | null {
  switch (key) {
    case "shape": return r.shape;
    case "partner": return r.opponent_name;
    case "asset": return r.asset_balance.ratio;
    case "verdict": return verdictRank(r.dual_value_summary?.verdict);   // backend order, best first; no verdict sorts last
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

/**
 * The backend query for a discovery page URL (`/trades/discover?team=&mode=&shape=…`). The same string goes to
 * `/api/trades/discover/context`, `/summary` and (with a row `key`) `/evidence`, so all three describe one view.
 * The page's default lens is `mutual_upgrades` (the API's own default stays `practical`), so a URL without `mode`
 * sends it explicitly; `?mode=practical` and `?mode=mutual` deep links pass through unchanged.
 */
export function discoverQuery(sp: SearchParams): URLSearchParams {
  const q = new URLSearchParams();
  const pass = (k: string, apiKey = k) => { const v = first(sp[k]); if (v) q.set(apiKey, v); };
  pass("team", "team_id");
  q.set("mode", first(sp.mode) || PAGE_DEFAULT_MODE);
  pass("phase");
  pass("strictness");
  pass("opponent");
  pass("category");
  pass("limit");
  // The API only takes its own sort keys, and only "All candidates" uses them (to choose which rows survive the limit).
  const sort = first(sp.sort);
  if (isApiSort(sort)) q.set("sort", sort);
  if (first(sp.kdst) === "1") q.set("include_kdst", "true");
  if (first(sp.exact) === "1") q.set("heuristic", "false");
  const shapes = sp.shape === undefined ? [] : Array.isArray(sp.shape) ? sp.shape : [sp.shape];
  if (shapes.length) q.set("shapes", shapes.join(","));
  for (const [k, v] of Object.entries(targetQuery(sp, q.get("mode") ?? PAGE_DEFAULT_MODE))) q.set(k, v);
  return q;
}

// --------------------------------------------------------------------------------- targeted discovery --

/** The page's URL keys for a targeted search (issue #182) and the API parameters they become. */
export const TARGET_URL_KEYS = { target: "target_player", pos: "target_position", need: "need", weeks: "weeks" } as const;

/**
 * The API's target parameters from a page URL. A roster need chooses for itself, so it wins over the matching manual
 * field: `need=weakest` drops `pos`, `need=bye` drops `weeks`. A horizon (`weeks`, `need=bye`) is only sent in the
 * targeted mode, the one that ranks by it; a player or position target filters any mode. A blank form field is no
 * target. The backend validates every value (an unknown player or week answers 422).
 */
export function targetQuery(sp: SearchParams, mode: string): Record<string, string> {
  const out: Record<string, string> = {};
  const v = (k: string) => { const x = first(sp[k])?.trim(); return x ? x : undefined; };
  const need = v("need");
  const targeted = mode === "targeted";
  if (v("target")) out.target_player = v("target")!;
  if (need && (need !== "bye" || targeted)) out.need = need;
  if (v("pos") && need !== "weakest") out.target_position = v("pos")!;
  if (targeted && v("weeks") && need !== "bye") out.weeks = v("weeks")!;
  return out;
}

/** The URL entries that keep a view's target when a link changes something else (from the API's query echo). */
export function targetHrefParams(query: { target_player?: number | null; target_position?: string | null; need?: string | null;
  weeks?: string | null }): Record<string, string | undefined> {
  return {
    target: query.target_player == null ? undefined : String(query.target_player),
    pos: query.target_position ?? undefined, need: query.need ?? undefined, weeks: query.weeks ?? undefined,
  };
}

/** Horizon choices for the targeted search: the phase (rest of season unless changed), next week, the next three. */
export function horizonOptions(start: number | null, final: number | null): { value: string; label: string }[] {
  const out = [{ value: "", label: "Season phase (rest of season unless set below)" }];
  if (start === null || final === null || final < start) return out;
  out.push({ value: String(start), label: `Next week (week ${start})` });
  const end = Math.min(start + 2, final);
  if (end > start) out.push({ value: `${start}-${end}`, label: `Next ${end - start + 1} weeks (weeks ${start}–${end})` });
  return out;
}

/** The analyzer URL that reproduces one discovered trade: both teams and both packages, nothing else. */
export function analyzerHref(r: { anchor_team_id: number; opponent_team_id: number; sends: number[]; receives: number[] }): string {
  const q = new URLSearchParams({ ta: String(r.anchor_team_id), tb: String(r.opponent_team_id) });
  r.sends.forEach((id) => q.append("a", String(id)));
  r.receives.forEach((id) => q.append("b", String(id)));
  return `/trades?${q.toString()}`;
}

/** Backend query parameters a row's evidence request may carry (everything `discoverQuery` sets, plus `key`). */
const EVIDENCE_PARAMS = ["team_id", "mode", "phase", "strictness", "opponent", "category", "limit", "sort", "include_kdst",
  "heuristic", "shapes", "target_player", "target_position", "need", "weeks", "key"] as const;

/** Backend path for one row's evidence, from the browser's `/api/trade-evidence?…` query; `null` without a row key. */
export function evidencePath(params: URLSearchParams): string | null {
  if (!params.get("key")) return null;
  const q = new URLSearchParams();
  for (const k of EVIDENCE_PARAMS) {
    const v = params.get(k);
    if (v) q.set(k, v);
  }
  return `/api/trades/discover/evidence?${q.toString()}`;
}

/** Browser URL for a row's evidence: the view's backend query plus the row key. */
export function evidenceSrc(query: string, key: string): string {
  const q = new URLSearchParams(query);
  q.set("key", key);
  return `/api/trade-evidence?${q.toString()}`;
}

/** The words that disclose a larger-shape search the work budget stopped early (`coverage.heuristic.work_budget`): a
 *  sentence for the "not a proven top list" notice and a note for the coverage tile. Null when the search ran to the
 *  end (the API then sends no `work_budget`). */
export function beamBudgetText(wb: BeamWorkBudget | null | undefined): { notice: string; tile: string } | null {
  if (!wb) return null;
  const cut = wb.opponents_cut.length;
  const evaluated = wb.packages_evaluated.toLocaleString("en-US");
  const generated = wb.packages_generated.toLocaleString("en-US");
  return {
    notice: `A work limit stopped it early for ${cut} of ${wb.opponents} ${wb.opponents === 1 ? "manager" : "managers"}: `
      + `${evaluated} of the ${generated} packages it generated were evaluated, so a better package may exist. `,
    tile: `work limit reached · ${evaluated} of ${generated} packages evaluated exactly`,
  };
}
