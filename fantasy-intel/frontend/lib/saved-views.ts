// Saved views (issue #200): named filter presets for the Rankings, Waivers and Trade discovery pages. A view is only
// the page's URL filter parameters, never data, and it lives only in this browser, under the canonical league ID and the
// tool it is for (`fi:saved-views:v1:<lg>:<tool>`), like the claim planner and the trade scenarios. Nothing is stored
// outside a league, and nothing reaches the server. The analyzed team is not part of a view, and neither is any
// parameter that only makes sense for one team (lib/team-perspective.ts TEAM_SPECIFIC_PARAMS: discovery's opponent, a
// page offset, an add/drop scenario), so a view does not depend on the team and is keyed by league and tool alone.
// Applying one keeps the team the page is analyzing (TeamLink).
//
// Every parameter goes through the tool's whitelist twice: when the current URL is captured, and again when a stored
// view is read back (a stored view with any unknown key or malformed value is refused whole). The pages still validate
// their own URL as before. No React and no `@/` imports, so node --test covers this module.

import { isLeagueId } from "./league-scope.ts";
import { RANK_HORIZONS, RANK_POSITIONS, RANKING_SORT_KEYS, ROSTER_FILTERS } from "./rankings.ts";
import { TABLE_SORTS } from "./trade-discovery.ts";
import { LEGACY_LENSES, LENSES } from "./trade-dual-value.ts";
import { WAIVER_SORT_KEYS } from "./waivers.ts";

export type ViewTool = "rankings" | "waivers" | "discover";
export const VIEW_TOOLS: readonly ViewTool[] = ["rankings", "waivers", "discover"];

/** A view's parameters as ordered URL entries (a repeated key, such as discovery's `shape`, appears once per value). */
export type ViewParams = [string, string][];
export type SavedView = { name: string; params: ViewParams };

/** At most this many saved views per league and tool. */
export const MAX_VIEWS = 12;
/** A view name's longest length, in characters (code points). */
export const MAX_NAME = 40;
/** The most values a repeated parameter keeps. */
const MAX_REPEAT = 6;

// The Waivers page's choices (app/waivers/helpers.ts POSITIONS, RECS, VIEWS; lib/saved-views.test.ts asserts they agree).
export const WAIVER_POSITIONS = ["QB", "RB", "WR", "TE", "K", "D/ST"] as const;
export const WAIVER_RECS = ["CLAIM", "WAIT", "IGNORE"] as const;
export const WAIVER_VIEWS = ["BEST", "FREE_AGENTS", "CLAIMS", "STREAMING", "WATCHLIST", "ALL"] as const;
// Trade discovery's fixed choices (backend app/analytics/trade_practical.py PHASES and STRICTNESS).
const DISCOVER_PHASES = ["ros", "regular", "playoff"] as const;
const DISCOVER_STRICTNESS = ["strict", "standard", "off"] as const;
const DISCOVER_POSITIONS = ["QB", "RB", "WR", "TE", "K", "D/ST"] as const;

type Rule = {
  /** True for an allowed value. */
  ok: (v: string) => boolean;
  /** Repeated key (several values kept, in order). */
  multi?: boolean;
  /** The value in words, for the view's summary line. */
  text?: (v: string) => string;
};

type ToolSpec = {
  /** The page the view opens. */
  path: string;
  /** The tool's name in copy ("Rankings views"). */
  label: string;
  /** Allowed parameters in their canonical order. Anything else (team, offset, add, drop, id, league) is never saved. */
  params: Record<string, Rule>;
  /** Named views every league starts with. */
  presets: SavedView[];
};

const among = (values: readonly string[]) => (v: string) => values.includes(v);
const int = (min: number, max: number) => (v: string) => /^\d{1,6}$/.test(v) && Number(v) >= min && Number(v) <= max;
const DIR: Rule = { ok: among(["asc", "desc"]), text: (v) => (v === "asc" ? "ascending" : "descending") };
/** A name search: trimmed, at most 60 characters, no control characters (the Rankings page's own cap). */
const SEARCH: Rule = { ok: (v) => v === v.trim() && v.length > 0 && Array.from(v).length <= 60 && !/\p{Cc}/u.test(v), text: (v) => `“${v}”` };
const word = (v: string) => v.toLowerCase().replaceAll("_", " ");

const SPECS: Record<ViewTool, ToolSpec> = {
  rankings: {
    path: "/rankings",
    label: "Rankings",
    params: {
      pos: { ok: among(RANK_POSITIONS.filter((p) => p !== "ALL")) },
      horizon: { ok: among(RANK_HORIZONS), text: (v) => (v === "ros" ? "Rest of season" : "This week") },
      source: { ok: among(["espn", "ours"]), text: (v) => (v === "ours" ? "Ours" : "ESPN projections") },
      status: { ok: among(ROSTER_FILTERS.filter((s) => s !== "all")), text: (v) => (v === "available" ? "Available" : "Rostered") },
      q: SEARCH,
      sort: { ok: among(RANKING_SORT_KEYS), text: (v) => `sorted by ${v === "xfp" ? "xFP/g" : v === "ppr" ? "PPR/g" : v}` },
      dir: DIR,
    },
    presets: [
      { name: "Available players", params: [["status", "available"]] },
      { name: "Rest of season by xFP/g", params: [["horizon", "ros"], ["sort", "xfp"], ["dir", "desc"]] },
    ],
  },
  waivers: {
    path: "/waivers",
    label: "Waivers",
    params: {
      view: { ok: among(WAIVER_VIEWS), text: (v) => ({
        BEST: "Best moves", FREE_AGENTS: "Free-agent adds", CLAIMS: "Waiver claims", STREAMING: "Streaming",
        WATCHLIST: "Watchlist", ALL: "All evaluated",
      })[v] ?? v },
      pos: { ok: among(WAIVER_POSITIONS) },
      rec: { ok: among(WAIVER_RECS), text: (v) => `${v[0]}${v.slice(1).toLowerCase()} only` },
      sort: { ok: among(WAIVER_SORT_KEYS), text: (v) => `sorted by ${v === "owned" ? "ownership change" : v}` },
      dir: DIR,
    },
    presets: [
      { name: "Claims across all players", params: [["view", "ALL"], ["rec", "CLAIM"]] },
      { name: "Rising roles", params: [["view", "ALL"], ["sort", "trend"], ["dir", "desc"]] },
    ],
  },
  discover: {
    path: "/trades/discover",
    label: "Trade discovery",
    params: {
      mode: { ok: among([...LENSES.map((l) => l.mode), ...Object.keys(LEGACY_LENSES)]),
        text: (v) => [...LENSES, ...Object.values(LEGACY_LENSES)].find((l) => l.mode === v)?.label ?? v },
      phase: { ok: among(DISCOVER_PHASES), text: (v) => ({ ros: "Rest of season", regular: "Regular season", playoff: "Fantasy playoffs" })[v] ?? v },
      strictness: { ok: among(DISCOVER_STRICTNESS), text: (v) => `${v} asset filter` },
      category: { ok: (v) => /^[a-z][a-z0-9_]{0,31}$/.test(v), text: word },
      limit: { ok: int(1, 50), text: (v) => `top ${v}` },
      sort: { ok: among(TABLE_SORTS), text: (v) => `sorted by ${word(v)}` },
      dir: DIR,
      kdst: { ok: among(["1"]), text: () => "with K and D/ST" },
      exact: { ok: among(["1"]), text: () => "exact search" },
      shape: { ok: (v) => /^[1-9]-for-[1-9]$/.test(v), multi: true },
      target: { ok: (v) => /^-?\d{1,12}$/.test(v) && v !== "0" && v !== "-0", text: () => "one player" },
      pos: { ok: among(DISCOVER_POSITIONS) },
      need: { ok: among(["weakest", "bye"]), text: (v) => (v === "bye" ? "bye-week cover" : "weakest starter") },
      weeks: { ok: (v) => /^\d{1,2}(-\d{1,2})?$/.test(v), text: (v) => `weeks ${v}` },
    },
    presets: [
      { name: "Fairer 1-for-1 swaps", params: [["mode", "fair"], ["shape", "1-for-1"]] },
      { name: "Playoff push", params: [["mode", "playoff"]] },
    ],
  },
};

export function viewTool(tool: ViewTool): { path: string; label: string; presets: SavedView[]; keys: string[] } {
  const s = SPECS[tool];
  return { path: s.path, label: s.label, presets: s.presets, keys: Object.keys(s.params) };
}

const isTool = (t: unknown): t is ViewTool => typeof t === "string" && (VIEW_TOOLS as readonly string[]).includes(t);

/** Entries in the spec's key order, each key's values in their given order. */
function canonical(tool: ViewTool, entries: ViewParams): ViewParams {
  const order = Object.keys(SPECS[tool].params);
  return [...entries].sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]));
}

/**
 * The view the page's URL shows: only whitelisted keys with allowed values, a single key once (its first value), a
 * repeated key at most MAX_REPEAT distinct values. Anything else (the team, the page offset, an add/drop scenario, a
 * malformed value) is left out rather than saved.
 */
export function viewParamsFromUrl(tool: ViewTool, sp: URLSearchParams | Iterable<[string, string]>): ViewParams {
  const rules = SPECS[tool].params;
  const out: ViewParams = [];
  const seen = new Map<string, Set<string>>();
  for (const [k, raw] of sp) {
    if (!Object.hasOwn(rules, k)) continue;
    const rule = rules[k];
    const v = k === "q" ? raw.trim() : raw;
    if (!rule.ok(v)) continue;
    const have = seen.get(k) ?? new Set<string>();
    if ((!rule.multi && have.size > 0) || have.has(v) || have.size >= MAX_REPEAT) continue;
    have.add(v);
    seen.set(k, have);
    out.push([k, v]);
  }
  return canonical(tool, out);
}

/**
 * A stored view's parameters, or null when any of them is not on the tool's whitelist: an unknown key, a bad value, a
 * single key given twice, a duplicated or excess repeated value. Strict, unlike the URL capture: a stored value that
 * fails is refused whole, never trimmed into a different view.
 */
export function validViewParams(tool: ViewTool, params: unknown): ViewParams | null {
  if (!Array.isArray(params) || params.length > 32) return null;
  const rules = SPECS[tool].params;
  const seen = new Map<string, Set<string>>();
  const out: ViewParams = [];
  for (const e of params as unknown[]) {
    if (!Array.isArray(e) || e.length !== 2 || typeof e[0] !== "string" || typeof e[1] !== "string") return null;
    const [k, v] = e as [string, string];
    if (!Object.hasOwn(rules, k) || !rules[k].ok(v)) return null;
    const have = seen.get(k) ?? new Set<string>();
    if ((!rules[k].multi && have.size > 0) || have.has(v) || have.size >= MAX_REPEAT) return null;
    have.add(v);
    seen.set(k, have);
    out.push([k, v]);
  }
  return canonical(tool, out);
}

/** True when two views select the same filters (key order aside; a repeated key's values compared as a set). */
export function sameView(a: ViewParams, b: ViewParams): boolean {
  const key = (p: ViewParams) => p.map(([k, v]) => `${k}=${v}`).sort().join("&");
  return key(a) === key(b);
}

/**
 * The page href for a view (no league, no team: the caller's TeamLink adds the analyzed team and the league). Every
 * parameter is validated again here, so even a caller holding an unchecked view cannot put another key in the link;
 * an invalid view opens the page's default.
 */
export function viewHref(tool: ViewTool, params: unknown): string {
  const ok = validViewParams(tool, params) ?? [];
  const qs = new URLSearchParams(ok).toString();
  return `${SPECS[tool].path}${qs ? `?${qs}` : ""}`;
}

/** The view's filters in words ("RB · Rest of season · Available"), or "Default view" for none. */
export function viewSummary(tool: ViewTool, params: ViewParams): string {
  const rules = SPECS[tool].params;
  const parts: string[] = [];
  for (const [k, v] of params) {
    const rule = rules[k];
    if (!rule) continue;
    const text = rule.text ? rule.text(v) : v;
    if (k === "dir" && parts.length > 0 && parts[parts.length - 1].startsWith("sorted by")) {
      parts[parts.length - 1] += `, ${text}`;
      continue;
    }
    parts.push(text);
  }
  return parts.length ? parts.join(" · ") : "Default view";
}

/** A typed view name: whitespace collapsed and trimmed, 1 to MAX_NAME characters, no control characters; else null. */
export function cleanViewName(raw: string): string | null {
  const name = raw.replace(/\s+/g, " ").trim();
  if (!name || Array.from(name).length > MAX_NAME || /\p{Cc}/u.test(name)) return null;
  return name;
}

const nameKey = (name: string) => name.normalize("NFKC").toLocaleLowerCase("en");

export type SaveResult =
  | { ok: true; views: SavedView[]; replaced: boolean; name: string }
  | { ok: false; error: string };

/**
 * Add a view, or replace the params of the view with the same name (case-insensitive), which keeps its place. Refuses
 * a bad name, a view with no filters (that is the page's default), a view that fails the whitelist and the cap.
 */
export function saveView(tool: ViewTool, views: readonly SavedView[], rawName: string, params: ViewParams): SaveResult {
  const name = cleanViewName(rawName);
  if (!name) return { ok: false, error: `Name the view (up to ${MAX_NAME} characters).` };
  const ok = validViewParams(tool, params);
  if (!ok) return { ok: false, error: "This page's filters cannot be saved as a view." };
  if (ok.length === 0) return { ok: false, error: "Choose at least one filter first: with none, this is the default view." };
  const i = views.findIndex((v) => nameKey(v.name) === nameKey(name));
  if (i >= 0) {
    const next = views.map((v, j) => (j === i ? { name, params: ok } : v));
    return { ok: true, views: next, replaced: true, name };
  }
  if (views.length >= MAX_VIEWS) return { ok: false, error: `You can keep up to ${MAX_VIEWS} views here. Delete one first.` };
  return { ok: true, views: [...views, { name, params: ok }], replaced: false, name };
}

export function removeView(views: readonly SavedView[], name: string): SavedView[] {
  return views.filter((v) => v.name !== name);
}

// ------------------------------------------------------------------------------------------- storage --

type Stored = { v: 1; league: string; tool: ViewTool; views: SavedView[] };

/** No shared fallback: views need a canonical league ID and a known tool, or they are not stored at all. */
export function savedViewsKey(leagueId: string | null | undefined, tool: ViewTool): string | null {
  return isLeagueId(leagueId) && isTool(tool) ? `fi:saved-views:v1:${leagueId}:${tool}` : null;
}

/**
 * The stored views, or none. The payload repeats its league and tool, so a value copied under another league's (or
 * tool's) key is refused rather than carried over. A malformed payload, an invalid name or parameter, a duplicate name
 * or more than MAX_VIEWS views is refused whole.
 */
export function parseSavedViews(raw: string | null, leagueId: string, tool: ViewTool): SavedView[] {
  try {
    const v = JSON.parse(raw ?? "null") as Partial<Stored> | null;
    if (!v || typeof v !== "object" || v.v !== 1 || v.league !== leagueId || v.tool !== tool || !Array.isArray(v.views)) return [];
    if (v.views.length > MAX_VIEWS) return [];
    const names = new Set<string>();
    const out: SavedView[] = [];
    for (const item of v.views as unknown[]) {
      if (!item || typeof item !== "object" || Array.isArray(item)) return [];
      const { name, params } = item as Record<string, unknown>;
      if (typeof name !== "string" || cleanViewName(name) !== name || names.has(nameKey(name))) return [];
      const ok = validViewParams(tool, params);
      if (!ok || ok.length === 0) return [];
      names.add(nameKey(name));
      out.push({ name, params: ok });
    }
    return out;
  } catch {
    return [];
  }
}

export function serializeSavedViews(leagueId: string, tool: ViewTool, views: SavedView[]): string {
  return JSON.stringify({ v: 1, league: leagueId, tool, views } satisfies Stored);
}

export function readSavedViews(storage: Pick<Storage, "getItem">, leagueId: string | null, tool: ViewTool): { views: SavedView[]; available: boolean } {
  const key = savedViewsKey(leagueId, tool);
  if (!key) return { views: [], available: false };
  try {
    return { views: parseSavedViews(storage.getItem(key), leagueId!, tool), available: true };
  } catch {
    return { views: [], available: false };
  }
}

export function writeSavedViews(storage: Pick<Storage, "setItem" | "removeItem">, leagueId: string | null, tool: ViewTool, views: SavedView[]): boolean {
  const key = savedViewsKey(leagueId, tool);
  if (!key) return false;
  try {
    if (views.length === 0) storage.removeItem(key);
    else storage.setItem(key, serializeSavedViews(leagueId!, tool, views));
    return true;
  } catch {
    return false;
  }
}
