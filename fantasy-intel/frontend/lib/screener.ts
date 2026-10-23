// Advanced player screener (issue #226): the page's URL state, the backend query it compiles to, and display helpers.
// Pure (no Next or React imports), so node --test covers it. The backend (GET /api/screener, contract screener/v1)
// validates everything again: this module only keeps the URL and the form in step and never invents a number.
//
// Page URL (a plain GET form, so it works without JavaScript and every screen is a shareable link):
//   pos=WR&pos=TE  from=1  to=8  last=3  min_games=3  min_routes=100  owner=available  sort=xfp_pg  dir=desc  limit=50
//   one filter row = the same index in fg (group 1-4), fm (metric key), fo (gte|gt|lte|lt) and fv (value, in the
//   metric's unit). A row with no metric is ignored; a row with a metric and a bad value stops the screen.
// Backend query: the same scalar params, and each row as f=<group>:<metric>:<op>:<value>.

import { plural } from "./plural.ts";

export const CONTRACT = "screener/v1";
export const PATH = "/screener";
export const POSITIONS = ["WR", "RB", "TE"] as const;
export type Position = (typeof POSITIONS)[number];
export const OPS = ["gte", "gt", "lte", "lt"] as const;
export type Op = (typeof OPS)[number];
export const OP_TEXT: Record<Op, string> = { gte: "≥", gt: ">", lte: "≤", lt: "<" };
export const OWNERS = ["any", "available", "rostered"] as const;
export type Owner = (typeof OWNERS)[number];
export const GROUP_NAMES = ["A", "B", "C", "D"] as const;
export const MAX_FILTERS = 8;
export const MAX_GROUPS = 4;
export const LIMITS = [25, 50, 100] as const;
export const DEFAULT_SORT = "xfp_pg";
/** Metric columns shown beside the filtered ones when there is room. */
export const DEFAULT_COLUMNS = ["xfp_pg", "ppr_pg", "fpoe_pg", "targets_pg"] as const;
export const MAX_METRIC_COLUMNS = 7;

const METRIC_KEY = /^[a-z][a-z0-9_]{0,31}$/;
const VALUE = /^-?[0-9]{1,4}(?:\.[0-9]{1,2})?$/;
const WHOLE = /^[0-9]{1,4}$/;

export type SearchParams = Record<string, string | string[] | undefined>;

export type FilterRow = { group: number; metric: string; op: Op; value: string };

export type ScreenerState = {
  pos: Position[];
  from: number | null;
  to: number | null;
  last: number | null;
  minGames: number | null;
  minRoutes: number | null;
  rows: FilterRow[];
  owner: Owner;
  sort: string | null;
  dir: "asc" | "desc" | null;
  limit: number | null;
  /** Problems that stop the screen from running (shown to the reader; the backend is never asked). */
  errors: string[];
};

const all = (v: string | string[] | undefined): string[] => (v === undefined ? [] : Array.isArray(v) ? v : [v]);
const first = (v: string | string[] | undefined): string | undefined => all(v)[0];

function whole(v: string | undefined, lo: number, hi: number): number | null {
  if (v === undefined || v.trim() === "" || !WHOLE.test(v.trim())) return null;
  const n = Number(v.trim());
  return n >= lo && n <= hi ? n : null;
}

/** The screen in the URL. Unknown or malformed scalars fall back to the backend defaults (and say so in `errors`). */
export function parseState(sp: SearchParams): ScreenerState {
  const errors: string[] = [];
  const pos = POSITIONS.filter((p) => all(sp.pos).includes(p));
  const num = (key: string, label: string, lo: number, hi: number): number | null => {
    const raw = first(sp[key]);
    if (raw === undefined || raw.trim() === "") return null;
    const v = whole(raw, lo, hi);
    if (v === null) errors.push(`${label} must be a whole number from ${lo} to ${hi}.`);
    return v;
  };
  const from = num("from", "First week", 1, 18);
  const to = num("to", "Last week", 1, 18);
  if (from !== null && to !== null && from > to) errors.push("The first week must not be after the last week.");
  const last = num("last", "Last games", 1, 8);
  const minGames = num("min_games", "Minimum games", 1, 18);
  const minRoutes = num("min_routes", "Minimum routes", 1, 1000);
  const fg = all(sp.fg), fm = all(sp.fm), fo = all(sp.fo), fv = all(sp.fv);
  const rows: FilterRow[] = [];
  const n = Math.max(fg.length, fm.length, fo.length, fv.length);
  for (let i = 0; i < n && i < 32; i += 1) {
    const metric = (fm[i] ?? "").trim();
    if (!metric) continue;
    const group = Number((fg[i] ?? "1").trim() || "1");
    const op = ((fo[i] ?? "gte").trim() || "gte") as Op;
    const value = (fv[i] ?? "").trim();
    const label = `Filter ${rows.length + 1}`;
    if (!METRIC_KEY.test(metric)) errors.push(`${label}: unknown metric.`);
    else if (!Number.isInteger(group) || group < 1 || group > MAX_GROUPS) errors.push(`${label}: group must be ${GROUP_NAMES.join(", ")}.`);
    else if (!(OPS as readonly string[]).includes(op)) errors.push(`${label}: unknown comparison.`);
    else if (!VALUE.test(value)) errors.push(`${label}: enter a number (up to 2 decimals) in the metric's unit.`);
    rows.push({ group: Number.isInteger(group) && group >= 1 && group <= MAX_GROUPS ? group : 1, metric, op: (OPS as readonly string[]).includes(op) ? op : "gte", value });
  }
  if (rows.length > MAX_FILTERS) errors.push(`At most ${MAX_FILTERS} filters.`);
  const owner = (OWNERS as readonly string[]).includes(first(sp.owner) ?? "") ? (first(sp.owner) as Owner) : "any";
  const sortRaw = first(sp.sort);
  const sort = sortRaw && METRIC_KEY.test(sortRaw) ? sortRaw : null;
  const dirRaw = first(sp.dir);
  const dir = dirRaw === "asc" || dirRaw === "desc" ? dirRaw : null;
  const limitRaw = whole(first(sp.limit), 1, 100);
  return { pos, from, to, last, minGames, minRoutes, rows, owner, sort, dir, limit: limitRaw, errors };
}

/** `group:metric:op:value` for the backend. */
export function filterToken(r: FilterRow): string {
  return `${r.group}:${r.metric}:${r.op}:${r.value}`;
}

/** The backend query string (`/api/screener?...`) for a state without errors. */
export function apiQuery(s: ScreenerState): string {
  const q = new URLSearchParams();
  for (const p of s.pos) q.append("pos", p);
  const set = (k: string, v: number | string | null) => { if (v !== null && v !== "") q.set(k, String(v)); };
  set("from", s.from);
  set("to", s.to);
  set("last", s.last);
  set("min_games", s.minGames);
  set("min_routes", s.minRoutes);
  for (const r of s.rows) q.append("f", filterToken(r));
  if (s.owner !== "any") q.set("owner", s.owner);
  set("sort", s.sort);
  set("dir", s.dir);
  set("limit", s.limit);
  return q.toString();
}

/** A preset's backend query (screener/v1 `presets[].query`) as page params: the page href under PATH. */
export type PresetQuery = {
  pos?: string[]; last?: number | null; min_games?: number | null; min_routes?: number | null; f?: string[];
  owner?: string; sort?: string; dir?: string;
};

export function presetHref(query: PresetQuery): string {
  const q = new URLSearchParams();
  for (const p of query.pos ?? []) q.append("pos", p);
  if (query.last) q.set("last", String(query.last));
  if (query.min_games) q.set("min_games", String(query.min_games));
  if (query.min_routes) q.set("min_routes", String(query.min_routes));
  for (const token of query.f ?? []) {
    const [g, m, o, v] = token.split(":");
    q.append("fg", g);
    q.append("fm", m);
    q.append("fo", o);
    q.append("fv", v);
  }
  if (query.owner && query.owner !== "any") q.set("owner", query.owner);
  if (query.sort) q.set("sort", query.sort);
  if (query.dir) q.set("dir", query.dir);
  const s = q.toString();
  return s ? `${PATH}?${s}` : PATH;
}

/** True when the page's screen is the preset's (same filters, positions and options, in any filter order). */
export function isPreset(s: ScreenerState, query: PresetQuery): boolean {
  const mine = [...s.rows.map(filterToken)].map(normalToken).sort().join("|");
  const theirs = [...(query.f ?? [])].map(normalToken).sort().join("|");
  const pos = (p: readonly string[]) => [...p].sort().join(",") || "WR,RB,TE".split(",").sort().join(",");
  return mine === theirs && pos(s.pos) === pos(query.pos ?? []) && (s.last ?? null) === (query.last ?? null)
    && (s.minGames ?? 1) === (query.min_games ?? 1) && s.owner === (query.owner ?? "any");
}

function normalToken(t: string): string {
  const [g, m, o, v] = t.split(":");
  const n = Number(v);
  return `${g}:${m}:${o}:${Number.isFinite(n) ? String(n) : v}`;
}

/** The form's rows: the screen's filters, then empty rows up to two more (never past MAX_FILTERS). */
export function formRows(s: ScreenerState): (FilterRow | null)[] {
  const rows: (FilterRow | null)[] = [...s.rows.slice(0, MAX_FILTERS)];
  const blanks = Math.min(2, MAX_FILTERS - rows.length);
  for (let i = 0; i < blanks; i += 1) rows.push(null);
  return rows;
}

// ---- the backend contract -----------------------------------------------------------------------------------------

export type Unit = "percent" | "per_game" | "points_per_game";

export type MetricInfo = {
  key: string; label: string; short: string; positions: Position[]; unit: Unit; unit_label: string;
  aggregation: string; inputs: string[]; denominator: string; source: string; bounds: [number, number];
  definition: string;
};

export type MetricCell = { value: number | null; n: number; of: number; reason?: string; percentile?: number; pool?: number };

export type Ownership = {
  status: "rostered" | "free_agent" | "waivers" | "not_rostered" | "unknown";
  label: string; available: boolean; player_id: number | null; mapping: string | null; team_id: number | null;
  team_name: string | null; is_mine: boolean; reason?: string | null;
};

export type ScreenRow = {
  gsis_id: string; name: string; position: Position; team: string | null; games: number; weeks: number[];
  routes: { total: number | null; n: number; of: number };
  metrics: Record<string, MetricCell>;
  conditions: { filter: string; result: "true" | "false" | "unknown" | "not_applicable" }[];
  incomplete: { metric: string; n: number; of: number; state: "missing" | "partial" }[];
  ownership: Ownership;
};

export type IncompleteRow = { gsis_id: string; name: string; position: Position; team: string | null; games: number; missing: string[]; reason: string };

export type Preset = { key: string; title: string; summary: string; query: PresetQuery };

export type Limits = {
  max_filters: number; max_groups: number; max_limit: number; default_limit: number; max_last: number; max_week: number;
  max_min_games: number; max_min_routes: number; percentile_min_pool: number; incomplete_cap: number;
  precedence: string; missing: string; rounding: string;
};

export type MetricsResponse = { contract: string; metrics: MetricInfo[]; limits: Limits; presets: Preset[] };

export type ScreenResponse = Omit<MetricsResponse, "presets"> & {
  status: "ok" | "empty" | "no_data";
  season: number | null;
  message?: string;
  expression: string | null;
  query: { season: number | null; pos: Position[]; from: number | null; to: number | null; last: number | null; min_games: number; min_routes: number | null; f: string[]; owner: Owner; sort: string; dir: "asc" | "desc"; limit: number };
  data: null | {
    season: number; latest_week: number | null; xfp_through_week: number | null; weeks_with_data: number[];
    nfl_synced_at: string | null; team_totals_source: string; scoring_basis: string;
    window?: { from: number; to: number; last: number | null };
    cutoffs?: Record<string, number | null>;
    route_data?: { rows: number; providers: string[]; updated_at: string | null; note: string | null };
  };
  counts: null | {
    pool: number; below_min_games: number; below_min_routes: number; routes_unknown: number; matched: number;
    not_matched: number; incomplete: number; ownership_unknown: number; ownership_excluded: number; shown: number;
  };
  truncated?: boolean;
  rows: ScreenRow[];
  incomplete: IncompleteRow[];
  incomplete_truncated?: boolean;
  ownership: null | { available: boolean; reason?: string; provider?: string; league_name?: string | null; my_team_id?: number | null; rosters_synced_at?: string | null; rule?: string };
};

// ---- display ------------------------------------------------------------------------------------------------------

/** A metric value in its unit: "71.25%" for shares, "5.33" for per-game numbers. Null stays null (the page shows —).
 * Two decimals for every unit, the precision the backend filters on (limits.rounding), so a value shown as "22.00%" is
 * one that passes "≥ 22" and "21.96%" visibly does not. */
export function formatValue(v: number | null | undefined, unit: Unit): string | null {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  return `${v.toFixed(2)}${unit === "percent" ? "%" : ""}`;
}

/** The filter as words: "Target share ≥ 22%". */
export function filterText(r: FilterRow, metric: MetricInfo | undefined): string {
  const name = metric?.short ?? r.metric;
  return `${name} ${OP_TEXT[r.op]} ${r.value}${metric?.unit === "percent" ? "%" : ""}`;
}

/** The metric columns of the results: filtered metrics, then the sort metric, then defaults (no duplicates, capped). */
export function columns(s: ScreenerState, sort: string, known: readonly MetricInfo[]): MetricInfo[] {
  const by = new Map(known.map((m) => [m.key, m]));
  const keys: string[] = [];
  for (const k of [...s.rows.map((r) => r.metric), sort, ...DEFAULT_COLUMNS]) {
    if (by.has(k) && !keys.includes(k)) keys.push(k);
  }
  return keys.slice(0, MAX_METRIC_COLUMNS).map((k) => by.get(k)!);
}

/** "3 of 4 games" for a partly covered cell, null when every game counted. */
export function sampleNote(c: MetricCell | undefined): string | null {
  if (!c || c.n === c.of) return null;
  return `${c.n} of ${c.of} ${plural(c.of, "game")}`;
}

/** A cell's tooltip: the value's sample, the percentile basis and, when missing, why. */
export function cellTitle(c: MetricCell | undefined, m: MetricInfo): string {
  if (!c) return `${m.label} is not defined for this position`;
  if (c.value === null) return `${m.label} missing: ${c.reason ?? "no game in the window has the inputs"}. Never counted as 0.`;
  const parts = [`${m.label}, ${m.unit_label}, over ${c.n} of ${c.of} ${plural(c.of, "game")}`];
  if (c.percentile !== undefined && c.pool !== undefined) parts.push(`${ordinal(c.percentile)} percentile among ${c.pool} players at the position in this window`);
  return parts.join("; ");
}

export function ordinal(n: number): string {
  const v = n % 100;
  const s = v >= 11 && v <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[n % 10] ?? "th";
  return `${n}${s}`;
}

/** "Weeks 2-5" / "Week 3" / "Weeks 1, 3, 4" for the games a row used. */
export function weeksText(weeks: readonly number[]): string {
  if (!weeks.length) return "No games";
  const contiguous = weeks.every((w, i) => i === 0 || w === weeks[i - 1] + 1);
  if (weeks.length === 1) return `Week ${weeks[0]}`;
  return contiguous ? `Weeks ${weeks[0]}–${weeks[weeks.length - 1]}` : `Weeks ${weeks.join(", ")}`;
}

/** One sentence on what the screen covered and dropped, from the backend's counts. */
export function countsSentence(c: NonNullable<ScreenResponse["counts"]>): string {
  // The same number as the page's "Matched" figure: players left out by the ownership filter are counted apart.
  const parts = [`${c.matched - c.ownership_excluded} of ${c.pool} ${plural(c.pool, "player")} matched`];
  if (c.below_min_games) parts.push(`${c.below_min_games} below the minimum games`);
  if (c.below_min_routes) parts.push(`${c.below_min_routes} below the minimum routes`);
  if (c.incomplete) parts.push(`${c.incomplete} could not be decided (missing data)`);
  if (c.ownership_excluded) parts.push(`${c.ownership_excluded} more matched but left out by the ownership filter`);
  return `${parts.join("; ")}.`;
}

/** Where a row's add/drop simulation lives: only for a player this league lists as available. */
export function waiverHref(o: Ownership): string | null {
  return o.available && o.player_id !== null ? `/waivers?add=${o.player_id}#add-drop` : null;
}

/** The table's sort as hidden form fields, so it survives an Apply of the builder. */
export function carried(sp: SearchParams): [string, string][] {
  return (["sort", "dir"] as const).flatMap((k) => {
    const v = first(sp[k]);
    return v ? [[k, v] as [string, string]] : [];
  });
}
